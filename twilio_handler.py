"""
fixed_twilio_handler.py
-----------------
Manages Twilio call handling and WebSocket communication with improved database integration.
Bridges Twilio audio streams with OpenAI's realtime API.
"""

import json
import base64
import asyncio
import re
import websockets
from fastapi import WebSocket, Request
from fastapi.responses import HTMLResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Connect
from dateutil import parser
from datetime import datetime
import logging
from config import load_clean_config
from database import save_appointment, get_connection, get_patient_by_id
from openai_handler import initialize_openai_session

logger = logging.getLogger(__name__)
config = load_clean_config()

async def handle_incoming_call(request: Request):
    logger.info("Received incoming call from %s", request.client.host)
    response = VoiceResponse()
    host = request.url.hostname
    connect = Connect()
    connect.stream(url=f'wss://{host}/media-stream')
    response.append(connect)
    logger.info("Generated TwiML response for incoming call")
    return HTMLResponse(content=str(response), media_type="application/xml")

async def handle_media_stream(websocket: WebSocket):
    logger.info("Entering handle_media_stream")
    await websocket.accept()
    logger.info("WebSocket accepted")
    
    state = {
        "stream_sid": None,
        "latest_media_timestamp": 0,
        "last_assistant_item": None,
        "mark_queue": [],
        "response_start_timestamp_twilio": None,
        "accumulated_text": "",
        "audio_chunk_count": 0,
        "transcript_buffer": "",
        "is_listening": True,  # Ensure listening state
        "user_transcript": "",  # Store user's speech transcript
        "last_user_transcript": "",  # Store previous user speech for comparison
        "potential_date_mentioned": False,  # Flag to track if a date might have been mentioned
        "appointment_confirmed": False,  # Track if appointment has been confirmed
        "pending_slot": None  # Store pending slot for booking
    }
    
    # Get a connection for initial patient data loading
    db_conn = get_connection()
    logger.info("Database connection established for initial data loading")
    
    try:
        async with websockets.connect(
            'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01',
            extra_headers={
                "Authorization": f"Bearer {config['OPENAI_API_KEY']}",
                "OpenAI-Beta": "realtime=v1"
            }
        ) as openai_ws:
            logger.info("Connected to OpenAI WebSocket")
            patient_details = await initialize_openai_session(openai_ws, db_conn)
            logger.info("OpenAI session initialized with patient: %s", patient_details["name"])
            
            # Close initial connection after loading patient data
            if not db_conn.closed:
                db_conn.close()
                logger.info("Closed initial database connection after loading patient data")
            
            async def receive_from_twilio():
                nonlocal state
                logger.info("Starting receive_from_twilio")
                try:
                    async for message in websocket.iter_text():
                        data = json.loads(message)
                        
                        if data["event"] == "media" and openai_ws.open:
                            state["latest_media_timestamp"] = int(data["media"]["timestamp"])
                            audio_append = {
                                "type": "input_audio_buffer.append",
                                "audio": data["media"]["payload"]
                            }
                            await openai_ws.send(json.dumps(audio_append))
                            state["audio_chunk_count"] += 1
                            if state["audio_chunk_count"] % 50 == 0:
                                logger.debug("Forwarded audio chunk %d to OpenAI at timestamp %d", state["audio_chunk_count"], state["latest_media_timestamp"])
                        
                        elif data["event"] == "start":
                            state["stream_sid"] = data["start"]["streamSid"]
                            logger.info("Twilio stream started: %s", state["stream_sid"])
                        
                        elif data["event"] == "mark":
                            if state["mark_queue"]:
                                state["mark_queue"].pop(0)
                                logger.debug("Processed mark event")
                        
                        elif data["event"] == "stop":
                            logger.info("Twilio stream stopped by stop event")
                            if openai_ws.open:
                                await openai_ws.close()
                            return
                except WebSocketDisconnect:
                    logger.info("Twilio WebSocket disconnected")
                    if openai_ws.open:
                        await openai_ws.close()
                except Exception as e:
                    logger.error("Error in receive_from_twilio: %s", str(e))
                    raise
                finally:
                    logger.info("receive_from_twilio ended")
            
            async def send_to_twilio():
                nonlocal state
                logger.info("Starting send_to_twilio")
                try:
                    async for message in openai_ws:
                        response = json.loads(message)
                        logger.info("OpenAI event: %s", response.get("type"))
                        
                        # Handle user speech transcript
                        if response.get("type") == "input_audio_buffer.transcript.delta":
                            user_speech = response.get("transcript", "")
                            state["user_transcript"] += user_speech
                            logger.info("User speech transcript delta: %s", user_speech)
                            
                            # Check for potential date mentions in user speech
                            if any(keyword in state["user_transcript"].lower() for keyword in 
                                  ["monday", "tuesday", "wednesday", "thursday", "friday", 
                                   "saturday", "sunday", "tomorrow", "next week", "january", 
                                   "february", "march", "april", "may", "june", "july", 
                                   "august", "september", "october", "november", "december"]):
                                state["potential_date_mentioned"] = True
                                logger.info("Potential date detected in user speech: %s", state["user_transcript"])
                        
                        # When user finishes speaking, process the complete transcript
                        elif response.get("type") == "input_audio_buffer.transcript.done":
                            if state["user_transcript"] and state["user_transcript"] != state["last_user_transcript"]:
                                logger.info("Complete user transcript: %s", state["user_transcript"])
                                
                                # Process potential appointment date/time
                                if state["potential_date_mentioned"]:
                                    await process_appointment_request(state["user_transcript"], patient_details, openai_ws, websocket, state)
                                
                                # Save current transcript as last transcript
                                state["last_user_transcript"] = state["user_transcript"]
                                state["user_transcript"] = ""
                                state["potential_date_mentioned"] = False
                        
                        # Handle AI audio response
                        elif response.get("type") == "response.audio.delta" and "delta" in response:
                            audio_payload = base64.b64encode(
                                base64.b64decode(response["delta"])
                            ).decode("utf-8")
                            await websocket.send_json({
                                "event": "media",
                                "streamSid": state["stream_sid"],
                                "media": {"payload": audio_payload}
                            })
                            logger.debug("Sent audio to Twilio")
                            if state["response_start_timestamp_twilio"] is None:
                                state["response_start_timestamp_twilio"] = state["latest_media_timestamp"]
                            if response.get("item_id"):
                                state["last_assistant_item"] = response["item_id"]
                            await send_mark(websocket, state["stream_sid"])
                        
                        # Handle AI text transcript
                        elif response.get("type") == "response.audio_transcript.delta":
                            state["transcript_buffer"] += response.get("transcript", "")
                            logger.debug("Transcript delta: %s", response.get("transcript", ""))
                        
                        # Process complete AI transcript
                        elif response.get("type") == "response.audio_transcript.done":
                            state["accumulated_text"] = state["transcript_buffer"]
                            logger.info("Full transcript: %s", state["accumulated_text"])
                            state["transcript_buffer"] = ""
                            
                            # Check if AI response indicates appointment confirmation
                            if not state["appointment_confirmed"]:
                                await check_for_appointment_confirmation(state["accumulated_text"], patient_details, openai_ws, websocket, state)
                        
                        # Handle AI response completion
                        elif response.get("type") == "response.done":
                            full_text = state["accumulated_text"].lower()
                            logger.info("AI response text: %s", full_text)
                            
                            # Reset accumulated text for next response
                            state["accumulated_text"] = ""
                        
                        # Handle user speech detection
                        elif response.get("type") == "input_audio_buffer.speech_started":
                            logger.info("User speech detected at timestamp %d", state["latest_media_timestamp"])
                            if state["last_assistant_item"]:
                                await handle_interruption(openai_ws, websocket, state)
                        
                        # Handle user speech completion
                        elif response.get("type") == "input_audio_buffer.speech_finished":
                            logger.info("User speech finished, processing")
                            state["is_listening"] = True
                
                except Exception as e:
                    logger.error("Error in send_to_twilio: %s", str(e))
                    await send_text(openai_ws, "Sorry, I hit a snag. Let's try again.")
                    raise
                finally:
                    logger.info("send_to_twilio ended")
            
            async def process_appointment_request(transcript, patient_details, openai_ws, twilio_ws, state):
                """Process user's appointment request from transcript."""
                logger.info("Processing potential appointment request: %s", transcript)
                
                # Extract date information using multiple patterns
                date_patterns = [
                    # Standard date formats
                    r'(\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})',
                    r'((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})',
                    r'(\d{4}-\d{1,2}-\d{1,2})',
                    r'(\d{1,2}/\d{1,2}/\d{4})',
                    r'(\d{1,2}-\d{1,2}-\d{4})',
                    
                    # Day references
                    r'(next\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday))',
                    r'(this\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday))',
                    r'(tomorrow)',
                    r'(the\s+day\s+after\s+tomorrow)',
                    
                    # Month and day without year
                    r'((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}(?:st|nd|rd|th)?)',
                    r'(\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:January|February|March|April|May|June|July|August|September|October|November|December))',
                    
                    # Numeric dates without year
                    r'(\d{1,2}/\d{1,2})',
                    r'(\d{1,2}-\d{1,2})'
                ]
                
                # Try to extract date using patterns
                extracted_date = None
                for pattern in date_patterns:
                    match = re.search(pattern, transcript, re.IGNORECASE)
                    if match:
                        extracted_date = match.group(1)
                        logger.info("Extracted date using pattern: %s", extracted_date)
                        break
                
                if not extracted_date:
                    logger.info("No specific date pattern found in transcript")
                    return
                
                # Try to parse the extracted date
                try:
                    # Handle relative dates
                    current_date = datetime.now()
                    if "tomorrow" in extracted_date.lower():
                        parsed_date = current_date.replace(day=current_date.day + 1)
                    elif "next" in extracted_date.lower():
                        # Simple approximation - add 7 days for next week
                        parsed_date = current_date.replace(day=current_date.day + 7)
                    elif "this" in extracted_date.lower():
                        # Simple approximation - add 2-3 days for this week
                        parsed_date = current_date.replace(day=current_date.day + 3)
                    elif "day after tomorrow" in extracted_date.lower():
                        parsed_date = current_date.replace(day=current_date.day + 2)
                    else:
                        # For dates without year, add current year
                        if not re.search(r'\d{4}', extracted_date):
                            extracted_date += f", {current_date.year}"
                        
                        parsed_date = parser.parse(extracted_date)
                    
                    normalized_date = parsed_date.strftime("%Y-%m-%d")
                    logger.info("Normalized date: %s", normalized_date)
                    
                    # Check if this date is in available slots
                    available_slots = [s for s in patient_details["availability"] if s["date"] == normalized_date]
                    
                    if available_slots:
                        # Found matching slots
                        slot_descriptions = "\n".join([f"- {s['start_time']} to {s['end_time']}" for s in available_slots])
                        confirmation_message = (
                            f"I found available slots on {normalized_date}:\n{slot_descriptions}\n"
                            f"Would you like me to book the {available_slots[0]['start_time']} slot for you?"
                        )
                        
                        # Store the first available slot as the one to book
                        state["pending_slot"] = available_slots[0]
                        print(f"DEBUG: Set pending_slot to {available_slots[0]}")
                        await send_text(openai_ws, confirmation_message)
                    else:
                        # No slots available on that date
                        alternative_dates = list(set([s["date"] for s in patient_details["availability"]]))[:3]
                        alternative_msg = (
                            f"I'm sorry, but there are no available slots on {normalized_date}. "
                            f"We do have openings on: {', '.join(alternative_dates)}. "
                            f"Would any of those work for you?"
                        )
                        await send_text(openai_ws, alternative_msg)
                
                except Exception as e:
                    logger.error("Error processing date: %s", str(e))
                    await send_text(openai_ws, "I'm having trouble understanding that date. Could you please specify the date again?")
            
            async def check_for_appointment_confirmation(transcript, patient_details, openai_ws, twilio_ws, state):
                """Check if AI response indicates appointment confirmation and process it."""
                transcript_lower = transcript.lower()
                

                # Debug logging
                print(f"DEBUG: Checking for appointment confirmation in: {transcript_lower}")
                with open("confirmation_debug.log", "a") as f:
                    f.write(f"{datetime.now()}: Checking transcript: {transcript_lower}\n")

                # Check for confirmation phrases in AI response
                scheduling_phrases = [
                    "scheduled your appointment",
                    "booked your appointment",
                    "confirmed your appointment",
                    "successfully scheduled",
                    "appointment is set for",
                    "i have scheduled your appointment for",
                    "your appointment is confirmed for",
                    "successfully booked for"
                ]

                    # Debug logging for phrases
                for phrase in scheduling_phrases:
                    if phrase in transcript_lower:
                        print(f"DEBUG: Found confirmation phrase: {phrase}")
                        with open("confirmation_debug.log", "a") as f:
                            f.write(f"{datetime.now()}: Found phrase: {phrase}\n")
                
                if any(phrase in transcript_lower for phrase in scheduling_phrases):
                    logger.info("Detected scheduling confirmation in AI response: %s", transcript)
                    
                    # Check if we have a pending slot from user request
                    if "pending_slot" in state and state["pending_slot"]:
                        logger.info("Using pending slot for confirmation: %s", state["pending_slot"])
                        confirmed_slot = state["pending_slot"]
                        
                        # Save the appointment using the robust database module
                        try:
                            # Use the robust database module to save the appointment
                            success = save_appointment(confirmed_slot["slot_id"])
                            
                            if success:
                                logger.info("Saved appointment for slot ID %s", confirmed_slot["slot_id"])
                                
                                # Mark appointment as confirmed
                                state["appointment_confirmed"] = True
                                
                                # Clear pending slot
                                state["pending_slot"] = None
                                
                                # Send confirmation message
                                await twilio_ws.send_json({
                                    "event": "media",
                                    "streamSid": state["stream_sid"],
                                    "media": {
                                        "payload": base64.b64encode(
                                            b"Your appointment has been confirmed and saved to our system. Thank you!"
                                        ).decode("utf-8")
                                    }
                                })
                                
                                # Don't stop the call immediately - let the conversation continue naturally
                                logger.info("Appointment confirmed and saved to database")
                            else:
                                logger.error("Failed to save appointment")
                                await send_text(openai_ws, "I'm having trouble saving your appointment. Let's try again.")
                                
                        except Exception as e:
                            logger.error("Error saving appointment: %s", str(e))
                            await send_text(openai_ws, "I'm having trouble saving your appointment. Let's try again.")
                    
                    # If we don't have a pending slot but found a date in the confirmation
                    else:
                        # Extract date from confirmation
                        date_pattern = r"""
                            (\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s+to\s+\d{2}:\d{2})|
                            ([A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th),\s+\d{4}\s+from\s+\d{1,2}:\d{2}\s+[APM]{2}\s+to\s+\d{1,2}:\d{2}\s+[APM]{2})|
                            (\d{4}-\d{2}-\d{2})|
                            ([A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th),\s+\d{4})
                        """
                        match = re.search(date_pattern, transcript, re.VERBOSE | re.IGNORECASE)
                        
                        if match and state["stream_sid"]:
                            raw_date = match.group().strip()
                            logger.info("Extracted raw date from confirmation: %s", raw_date)
                            try:
                                date_part = raw_date.split(" from ")[0] if " from " in raw_date else raw_date
                                dt = parser.parse(date_part)
                                normalized_date = dt.strftime("%Y-%m-%d")
                                logger.info("Normalized date: %s", normalized_date)
                                
                                # Find matching slot
                                confirmed_slot = next(
                                    (s for s in patient_details["availability"] if s["date"] == normalized_date),
                                    None
                                )
                                
                                if confirmed_slot:
                                    logger.info("Found slot: %s", confirmed_slot)
                                    
                                    # Use the robust database module to save the appointment
                                    success = save_appointment(confirmed_slot["slot_id"])
                                    
                                    if success:
                                        logger.info("Saved appointment for slot ID %s", confirmed_slot["slot_id"])
                                        
                                        # Mark appointment as confirmed
                                        state["appointment_confirmed"] = True
                                        
                                        await twilio_ws.send_json({
                                            "event": "media",
                                            "streamSid": state["stream_sid"],
                                            "media": {
                                                "payload": base64.b64encode(
                                                    b"Your appointment has been confirmed and saved to our system. Thank you!"
                                                ).decode("utf-8")
                                            }
                                        })
                                        
                                        # Don't stop the call immediately - let the conversation continue naturally
                                        logger.info("Appointment confirmed and saved to database")
                                    else:
                                        logger.error("Failed to save appointment")
                                        await send_text(openai_ws, "I'm having trouble saving your appointment. Let's try again.")
                                else:
                                    logger.warning("No slot found for %s", normalized_date)
                                    await send_text(openai_ws, "I'm sorry, but I couldn't find that slot in our system. Let's try again with a different date.")
                            except Exception as e:
                                logger.error("Date parsing error: %s", str(e))
                                await send_text(openai_ws, "I'm having trouble processing that date. Could you please specify another date?")
            
            async def handle_interruption(openai_ws, twilio_ws, state):
                if state["mark_queue"] and state["response_start_timestamp_twilio"] is not None:
                    elapsed_time = state["latest_media_timestamp"] - state["response_start_timestamp_twilio"]
                    if state["last_assistant_item"]:
                        truncate_event = {
                            "type": "conversation.item.truncate",
                            "item_id": state["last_assistant_item"],
                            "content_index": 0,
                            "audio_end_ms": elapsed_time
                        }
                        await openai_ws.send(json.dumps(truncate_event))
                    await twilio_ws.send_json({
                        "event": "clear",
                        "streamSid": state["stream_sid"]
                    })
                    state["mark_queue"].clear()
                    state["last_assistant_item"] = None
                    state["response_start_timestamp_twilio"] = None
                    logger.info("AI response truncated")
            
            async def send_mark(connection, stream_sid):
                if stream_sid:
                    mark_event = {
                        "event": "mark",
                        "streamSid": stream_sid,
                        "mark": {"name": "responsePart"}
                    }
                    await connection.send_json(mark_event)
                    state["mark_queue"].append("responsePart")
                    logger.debug("Sent mark event")
            
            async def send_text(openai_ws, text):
                message = {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "input_text", "text": text}]
                    }
                }
                await openai_ws.send(json.dumps(message))
                await openai_ws.send(json.dumps({"type": "response.create"}))

            logger.info("Starting Twilio-OpenAI streaming")
            await asyncio.gather(receive_from_twilio(), send_to_twilio())
            logger.info("Streaming completed")
    
    except Exception as e:
        logger.error("Media stream handler failed: %s", str(e))
        raise

async def trigger_call():
    client = Client(config["TWILIO_ACCOUNT_SID"], config["TWILIO_AUTH_TOKEN"])
    logger.info("Using TWILIO_ACCOUNT_SID: %s", config["TWILIO_ACCOUNT_SID"])
    logger.info("Using TWILIO_AUTH_TOKEN: %s", config["TWILIO_AUTH_TOKEN"][:4] + "****")
    logger.info("Calling from %s to %s via %s", config["TWILIO_PHONE_NUMBER"], config["YOUR_PHONE_NUMBER"], config["HOSTNAME"])
    call = client.calls.create(
        url=f"https://{config['HOSTNAME']}/incoming-call",
        to=config["YOUR_PHONE_NUMBER"],
        from_=config["TWILIO_PHONE_NUMBER"]
    )
    logger.info("Initiated outbound call: %s", call.sid)
    return {"status": "Call initiated!", "call_sid": call.sid}
