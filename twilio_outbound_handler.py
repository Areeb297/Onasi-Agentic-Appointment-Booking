"""
twilio_outbound_handler.py
-----------------
Manages Twilio OUTBOUND call handling and WebSocket communication.
Bridges Twilio audio streams with OpenAI's realtime API for outbound scheduling.
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
from openai_handler import initialize_openai_session_outbound, translate_and_extract_appointment_info, initialize_openai_session_inbound

logger = logging.getLogger(__name__)
config = load_clean_config()

# Define typical AI closing phrases (lowercase)
AI_GOODBYE_PHRASES = [
    "have a great day",
    "look forward to seeing you",
    "thanks for calling",
    "goodbye",
    "take care"
]

async def handle_incoming_call(request: Request, stream_endpoint: str = "/media-stream"):
    """Generates TwiML to connect a call to a WebSocket media stream."""
    logger.info("Generating TwiML for call, streaming to endpoint: %s", stream_endpoint)
    response = VoiceResponse()
    host = request.url.hostname
    ws_protocol = "wss://" if request.url.scheme in ['https', 'wss'] else "ws://"
    stream_url = f"{ws_protocol}{host}{stream_endpoint}" # Use the provided endpoint
    connect = Connect()
    connect.stream(url=stream_url)
    response.append(connect)
    logger.info("Generated TwiML response for call, streaming to: %s", stream_url)
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
        "appointment_confirmed": False,
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
            patient_details = await initialize_openai_session_outbound(openai_ws, db_conn)
            logger.info(f"Value returned by initialize_openai_session_outbound: {patient_details}")
            logger.info(f"Type returned by initialize_openai_session_outbound: {type(patient_details)}")
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
                            audio_payload = data["media"]["payload"]
                            if not openai_ws.open:
                                continue

                            audio_append = {
                                "type": "input_audio_buffer.append",
                                "audio": audio_payload
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
                            logger.info("Received Twilio stream stop event - IGNORING and continuing loop.")
                        
                        elif data["event"] == "stop":
                            logger.info("Twilio stream stopped by stop event")
                            return # Exit the function
                except WebSocketDisconnect:
                    logger.info("Twilio WebSocket disconnected")
                    # We likely should close OpenAI WS here if Twilio disconnects
                    if openai_ws.open:
                        await openai_ws.close()
                        logger.info("Closed OpenAI WebSocket due to Twilio disconnect.")
                except Exception as e:
                    logger.error("Error in receive_from_twilio: %s", str(e))
                    if openai_ws.open:
                        await openai_ws.close() # Close OpenAI on other errors too
                    raise
                finally:
                    logger.info("receive_from_twilio loop ended") # Clarify log
            
            async def send_to_twilio():
                nonlocal state
                logger.info("Starting send_to_twilio listening loop.")
                try:
                    # Keep listening as long as the OpenAI WebSocket is open
                    while openai_ws.open:
                        try:
                            # Wait for the next message with a timeout 
                            message_json = await asyncio.wait_for(openai_ws.recv(), timeout=60.0) # 60s timeout
                            response = json.loads(message_json)

                            # Handle user speech transcript delta (Remove potential_date_mentioned logic)
                            if response.get("type") == "input_audio_buffer.transcript.delta":
                                user_speech = response.get("transcript", "")
                                state["user_transcript"] += user_speech
                                logger.info("User speech transcript delta: %s", user_speech)
                            
                            # When user finishes speaking, TRANSLATE then process
                            elif response.get("type") == "input_audio_buffer.transcript.done":
                                final_user_transcript = state["user_transcript"]
                                # Log the transcript value BEFORE the check
                                logger.info(f"Input transcript done event received. Transcript content: '{final_user_transcript}'")
                                # Simplify condition: Process if transcript is not empty
                                if final_user_transcript: 
                                    logger.info("Transcript is non-empty, proceeding with translation for date offering.")
                                    # --- Translation Step for User Request --- 
                                    # Use the same function, but we primarily care about the translation here
                                    extracted_info = await translate_and_extract_appointment_info(final_user_transcript)
                                    if extracted_info is None:
                                        logger.error("Translation/Extraction failed for user transcript.")
                                        # Reset state but don't necessarily error out the whole call yet
                                        state["last_user_transcript"] = final_user_transcript 
                                        state["user_transcript"] = ""
                                        continue 
                                    
                                    english_transcript_text = extracted_info.get("translation", "")
                                    logger.info(f"Translated user transcript (English): {english_transcript_text}")
                                    
                                    # --- Offer Matching Slots --- 
                                    # Call the function dedicated to handling user date requests
                                    await offer_matching_slots(english_transcript_text, patient_details, openai_ws)
                                    
                                    state["last_user_transcript"] = final_user_transcript 
                                    state["user_transcript"] = ""
                                else:
                                     logger.info("User transcript done event: skipping processing (transcript was empty).")
                                     state["user_transcript"] = "" # Clear buffer anyway
                                     # Ensure last_user_transcript doesn't get set to empty if it was already empty
                                     if final_user_transcript: 
                                         state["last_user_transcript"] = final_user_transcript
                            
                            # Handle AI audio response
                            elif response.get("type") == "response.audio.delta" and "delta" in response:
                                audio_payload = base64.b64encode(base64.b64decode(response["delta"]) ).decode("utf-8")
                                await websocket.send_json({"event": "media", "streamSid": state["stream_sid"], "media": {"payload": audio_payload}})
                                if state["response_start_timestamp_twilio"] is None:
                                    state["response_start_timestamp_twilio"] = state["latest_media_timestamp"]
                                if response.get("item_id"):
                                    state["last_assistant_item"] = response["item_id"]
                                await send_mark(websocket, state["stream_sid"])
                            
                            # Handle AI text transcript
                            elif response.get("type") == "response.audio_transcript.delta":
                                transcript_delta = response.get("transcript", "")
                                state["transcript_buffer"] += transcript_delta
                                # logger.info("Received AI transcript delta: %s", transcript_delta) # Removed info log
                            
                            # Process complete AI transcript
                            elif response.get("type") == "response.audio_transcript.done":
                                state["accumulated_text"] = response.get("transcript", state["transcript_buffer"])
                                logger.info("Full AI transcript accumulated: %s", state["accumulated_text"])
                                state["transcript_buffer"] = ""
                                # Check if this AI transcript confirms an appointment
                                if not state["appointment_confirmed"]:
                                    await check_for_appointment_confirmation(state["accumulated_text"], patient_details, openai_ws, websocket, state)
                                
                                # NOTE: Don't reset accumulated_text here yet
                            
                            # Handle AI response completion
                            elif response.get("type") == "response.done":
                                # Use the text accumulated during response.audio_transcript.done
                                full_text = state["accumulated_text"].lower() 
                                logger.info("AI response text finalized (in response.done): %s", full_text)
                                
                                # --- Check for Hangup Trigger AFTER checking confirmation ---
                                triggered_hangup = False
                                for phrase in AI_GOODBYE_PHRASES:
                                    if phrase in full_text:
                                        logger.info(f"Detected AI goodbye phrase: '{phrase}'. Waiting 5s before triggering call end.")
                                        await asyncio.sleep(5) # Use 5 seconds
                                        try:
                                            if openai_ws.open:
                                                await openai_ws.close()
                                                logger.info("Closed OpenAI WebSocket.")
                                            await websocket.close()
                                            logger.info("Closed Twilio WebSocket.")
                                            triggered_hangup = True
                                            break
                                        except Exception as close_err:
                                            logger.error(f"Error closing websockets: {close_err}")
                                
                                if triggered_hangup:
                                    logger.info("Hangup triggered, breaking send_to_twilio loop.")
                                    break # Exit the while loop
                                
                                # Reset accumulated text only if not hanging up
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
                                
                        except asyncio.TimeoutError:
                            logger.debug("OpenAI receive timeout, checking connection.")
                            if not openai_ws.open:
                                logger.warning("OpenAI WebSocket closed during timeout check.")
                                break 
                            continue # Continue waiting if open
                        except websockets.exceptions.ConnectionClosedOK:
                             logger.info("OpenAI WebSocket connection closed normally.")
                             break 
                        except websockets.exceptions.ConnectionClosedError as e:
                             logger.error(f"OpenAI WebSocket connection closed with error: {e}")
                             break
                        except Exception as loop_err:
                             logger.error(f"Error processing message in send_to_twilio loop: {loop_err}")
                             continue # Continue the loop

                except Exception as e:
                    logger.error("Error in send_to_twilio outer loop: %s", str(e))
                    # Ensure websockets are closed on outer exception
                    try:
                        if openai_ws.open: await openai_ws.close()
                        # Checking FastAPI websocket state before close is tricky, attempt close directly
                        await websocket.close()
                    except Exception as close_err:
                         logger.error(f"Error closing websockets during outer exception handling: {close_err}")
                    # raise # Optionally re-raise
                finally:
                    logger.info("send_to_twilio listening loop ended.")
            
            async def offer_matching_slots(english_transcript: str, patient_details: dict, openai_ws):
                 """Processes translated ENGLISH user transcript to find date and offer slots via AI."""
                 logger.info("--- Starting offer_matching_slots ---")
                 logger.info(f"Processing English transcript: '{english_transcript}'")
                 
                 english_months_pattern = 'January|February|March|April|May|June|July|August|September|October|November|December'
                 date_patterns = [
                     # ... (Standard English patterns) ...
                     rf'(\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{english_months_pattern})\s+\d{{4}})', 
                     rf'((?:{english_months_pattern})\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}})', 
                     # ... include other relevant patterns ...
                     r'(tomorrow)', r'(next\s+week)', # etc.
                 ]
                 
                 extracted_date_str = None
                 # ... (Loop through patterns to find extracted_date_str) ...
                 
                 if not extracted_date_str:
                     logger.info("No date pattern matched in user request.")
                     # Optionally send a clarification message back? 
                     # await send_text(openai_ws, "I didn't catch a specific date, could you please repeat?")                     
                     logger.info("--- Ending offer_matching_slots (no date pattern match) ---")
                     return
                 
                 date_to_parse = extracted_date_str
                 try:
                     # ... (Parse date_to_parse to get parsed_date object) ...
                     parsed_date = parser.parse(date_to_parse)
                     normalized_db_date = parsed_date.strftime("%Y-%m-%d")
                     logger.info(f"Parsed user request date to: {normalized_db_date}")
                     
                     all_available_slots = patient_details.get("availability", []) 
                     slots_on_date = [s for s in all_available_slots if s["date"] == normalized_db_date]
                     
                     if slots_on_date:
                         slot_descriptions = "\n".join([f"- {s['start_time']} to {s['end_time']}" for s in slots_on_date])
                         offer_message = (
                             f"Okay, for {normalized_db_date}, I found these available slots:\n{slot_descriptions}\n"
                             f"Would you like to book the first one at {slots_on_date[0]['start_time']}?"
                         )
                         await send_text(openai_ws, offer_message)
                     else:
                         if not all_available_slots: 
                             alternative_msg = f"I'm sorry, we don't have any openings on {normalized_db_date}, and there are currently no available appointment slots in our system at all. Please check back later."
                         else:
                             alternative_dates = list(set([s["date"] for s in all_available_slots]))[:3]
                             alternative_msg = f"I'm sorry, but there are no available slots on {normalized_db_date}. We do have openings on other dates like: {', '.join(alternative_dates)}. Would any of those work?"
                         await send_text(openai_ws, alternative_msg)
                 except Exception as e:
                     logger.error(f"Error parsing user date or finding slots: {e}")
                     await send_text(openai_ws, "I had trouble understanding that date. Could you please specify it again?")
                 finally:
                     logger.info("--- Ending offer_matching_slots ---")

            async def check_for_appointment_confirmation(ai_transcript: str, patient_details: dict, openai_ws, websocket, state):
                """Processes final AI transcript via LLM, finds slot, and saves."""
                logger.info(f"Checking final AI transcript for booking confirmation: '{ai_transcript[:100]}...'")
                
                # --- Translate and Extract Date/Time using LLM --- 
                extracted_info = await translate_and_extract_appointment_info(ai_transcript) 
                if not extracted_info:
                     logger.error("Translation/extraction of AI transcript failed.")
                     return

                extracted_date = extracted_info.get("date") # Expects YYYY-MM-DD
                extracted_time = extracted_info.get("time") # Expects HH:MM:SS
                logger.info(f"LLM Extraction Result: Date={extracted_date}, Time={extracted_time}")

                # --- Find Slot and Save --- 
                if extracted_date and extracted_time: 
                    logger.info("LLM extracted valid date and time. Attempting to find matching slot.")
                    availability = patient_details.get("availability", [])
                    found_slot_id = None
                    for slot in availability:
                        # Exact match on date and time (HH:MM:SS)
                        if slot.get('date') == extracted_date and slot.get('start_time') == extracted_time:
                            found_slot_id = slot.get('slot_id')
                            logger.info(f"Found matching slot_id: {found_slot_id}")
                            break
                    
                    if found_slot_id:
                        logger.info(f"Attempting to save appointment for slot_id: {found_slot_id}")
                        try:
                            success = save_appointment(found_slot_id)
                            logger.info(f"save_appointment returned: {success}")
                            if success:
                                logger.info("Saved appointment for slot ID %s", found_slot_id)
                                state["appointment_confirmed"] = True
                            else:
                                logger.error("Failed to save appointment (save_appointment returned False)")
                        except Exception as e:
                            logger.error("Error calling save_appointment: %s", str(e))
                    else:
                         logger.warning(f"LLM extracted date/time '{extracted_date} {extracted_time}', but no matching available slot found.")
                else:
                    logger.info("LLM did not extract a confirmable date/time from this AI transcript.")
            
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
    # Point the outbound call to the new TwiML endpoint
    call_twiml_url = f"https://{config['HOSTNAME']}/outbound-call-twiml"
    call = client.calls.create(
        url=call_twiml_url,
        to=config["YOUR_PHONE_NUMBER"],
        from_=config["TWILIO_PHONE_NUMBER"]
    )
    logger.info("Initiated outbound call (%s) pointing to TwiML URL: %s", call.sid, call_twiml_url)
    return {"status": "Call initiated!", "call_sid": call.sid} 