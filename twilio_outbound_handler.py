"""
twilio_outbound_handler.py
-----------------
Manages Twilio OUTBOUND call handling and WebSocket communication.
Bridges Twilio audio streams with OpenAI's realtime API for outbound scheduling.
Sends WhatsApp notifications on successful appointment bookings.
Supports unified inbound/outbound TwiML and streaming.
"""

import json
import base64
import asyncio
import re
import websockets
from fastapi import WebSocket, Request
from fastapi.responses import HTMLResponse
from fastapi.websockets import WebSocketDisconnect, WebSocketState
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Connect
from dateutil import parser
from datetime import datetime
import logging
from config import load_clean_config
from database import save_appointment, get_connection, get_patient_by_id
from openai_handler import initialize_openai_session_outbound, translate_and_extract_appointment_info
from openai_handler import client as openai_api_client
import requests # For downloading Twilio recording
import time # For polling delays
import os # For file operations like removing audio file

logger = logging.getLogger(__name__)
config = load_clean_config()

# Initialize Twilio client
twilio_client = Client(config["TWILIO_ACCOUNT_SID"], config["TWILIO_AUTH_TOKEN"])

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
    stream_url = f"{ws_protocol}{host}{stream_endpoint}"
    connect = Connect()
    connect.stream(url=stream_url)
    response.append(connect)
    logger.info("Generated TwiML response for call, streaming to: %s", stream_url)
    return HTMLResponse(content=str(response), media_type="application/xml")

async def handle_media_stream(websocket: WebSocket):
    logger.info("Entering handle_media_stream for call")
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
        "is_listening": True,
        "user_transcript": "",
        "last_user_transcript": "",
        "appointment_confirmed": False,
        "is_outbound": False,
        "call_sid": None
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
            # Initialize session (outbound-specific logic can be handled in openai_handler)
            patient_details = await initialize_openai_session_outbound(openai_ws, db_conn)
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
                            audio_append = {
                                "type": "input_audio_buffer.append",
                                "audio": audio_payload
                            }
                            logger.debug("Prepared audio_append for OpenAI: %s", json.dumps(audio_append).encode('utf-8')[:100])
                            await openai_ws.send(json.dumps(audio_append))
                            state["audio_chunk_count"] += 1
                            if state["audio_chunk_count"] % 50 == 0:
                                logger.debug("Forwarded audio chunk %d to OpenAI at timestamp %d", state["audio_chunk_count"], state["latest_media_timestamp"])
                        
                        elif data["event"] == "start":
                            state["stream_sid"] = data["start"]["streamSid"]
                            # Capture callSid if present (Twilio sends it here)
                            state["call_sid"] = data["start"].get("callSid") 
                            logger.info("Twilio stream started: %s (callSid: %s)", state["stream_sid"], state["call_sid"])
                            # Optionally detect call direction from Twilio data
                            # For simplicity, rely on initialization context
                        
                        elif data["event"] == "mark":
                            if state["mark_queue"]:
                                state["mark_queue"].pop(0)
                                logger.debug("Processed mark event")
                        
                        elif data["event"] == "stop":
                            logger.info("Twilio stream stopped")
                            return
                except WebSocketDisconnect:
                    logger.info("Twilio WebSocket disconnected")
                    if openai_ws.open:
                        await openai_ws.close()
                        logger.info("Closed OpenAI WebSocket due to Twilio disconnect")
                except Exception as e:
                    logger.error("Error in receive_from_twilio: %s", str(e))
                    if openai_ws.open:
                        await openai_ws.close()
                    raise
                finally:
                    logger.info("receive_from_twilio loop ended")
            
            async def send_to_twilio():
                nonlocal state
                logger.info("Starting send_to_twilio listening loop")
                try:
                    while openai_ws.open:
                        try:
                            message_json = await asyncio.wait_for(openai_ws.recv(), timeout=60.0)
                            response = json.loads(message_json)

                            if response.get("type") == "input_audio_buffer.transcript.delta":
                                user_speech = response.get("transcript", "")
                                state["user_transcript"] += user_speech
                                logger.info("User speech transcript delta: %s", user_speech)
                            
                            elif response.get("type") == "input_audio_buffer.transcript.done":
                                final_user_transcript = state["user_transcript"]
                                logger.info(f"Input transcript done event received. Transcript content: '{final_user_transcript}'")
                                if final_user_transcript:
                                    logger.info("Transcript is non-empty, proceeding with translation for date offering")
                                    extracted_info = await translate_and_extract_appointment_info(final_user_transcript)
                                    if extracted_info is None:
                                        logger.error("Translation/Extraction failed for user transcript")
                                        state["last_user_transcript"] = final_user_transcript 
                                        state["user_transcript"] = ""
                                        continue 
                                    
                                    english_transcript_text = extracted_info.get("translation", "")
                                    logger.info(f"Translated user transcript (English): {english_transcript_text}")
                                    await offer_matching_slots(english_transcript_text, patient_details, openai_ws)
                                    
                                    state["last_user_transcript"] = final_user_transcript 
                                    state["user_transcript"] = ""
                                else:
                                    logger.info("User transcript done event: skipping processing (transcript was empty)")
                                    state["user_transcript"] = ""
                                    if final_user_transcript:
                                        state["last_user_transcript"] = final_user_transcript
                            
                            elif response.get("type") == "response.audio.delta" and "delta" in response:
                                audio_payload = base64.b64encode(base64.b64decode(response["delta"])).decode("utf-8")
                                await websocket.send_json({"event": "media", "streamSid": state["stream_sid"], "media": {"payload": audio_payload}})
                                if state["response_start_timestamp_twilio"] is None:
                                    state["response_start_timestamp_twilio"] = state["latest_media_timestamp"]
                                if response.get("item_id"):
                                    state["last_assistant_item"] = response["item_id"]
                                await send_mark(websocket, state["stream_sid"])
                            
                            elif response.get("type") == "response.audio_transcript.delta":
                                transcript_delta = response.get("transcript", "")
                                state["transcript_buffer"] += transcript_delta
                            
                            elif response.get("type") == "response.audio_transcript.done":
                                state["accumulated_text"] = response.get("transcript", state["transcript_buffer"])
                                logger.info("Full AI transcript accumulated: %s", state["accumulated_text"])
                                state["transcript_buffer"] = ""
                                if not state["appointment_confirmed"]:
                                    await check_for_appointment_confirmation(state["accumulated_text"], patient_details, openai_ws, websocket, state)
                            
                            elif response.get("type") == "response.done":
                                full_text = state["accumulated_text"].lower()
                                logger.info("AI response text finalized (in response.done): %s", full_text)
                                
                                triggered_hangup = False
                                for phrase in AI_GOODBYE_PHRASES:
                                    if phrase in full_text:
                                        logger.info(f"Detected AI goodbye phrase: '{phrase}'. Waiting 5s before triggering call end")
                                        await asyncio.sleep(5)
                                        try:
                                            if openai_ws.open:
                                                await openai_ws.close()
                                                logger.info("Closed OpenAI WebSocket")
                                            await websocket.close()
                                            logger.info("Closed Twilio WebSocket")
                                            triggered_hangup = True
                                            break
                                        except Exception as close_err:
                                            logger.error(f"Error closing websockets: {close_err}")
                                
                                if triggered_hangup:
                                    logger.info("Hangup triggered, breaking send_to_twilio loop")
                                    break
                                
                                state["accumulated_text"] = ""

                            elif response.get("type") == "input_audio_buffer.speech_started":
                                logger.info("User speech detected at timestamp %d", state["latest_media_timestamp"])
                                if state["last_assistant_item"]:
                                    await handle_interruption(openai_ws, websocket, state)
                            
                            elif response.get("type") == "input_audio_buffer.speech_finished":
                                logger.info("User speech finished, processing")
                                state["is_listening"] = True
                                
                        except asyncio.TimeoutError:
                            logger.debug("OpenAI receive timeout, checking connection")
                            if not openai_ws.open:
                                logger.warning("OpenAI WebSocket closed during timeout check")
                                break
                            continue
                        except websockets.exceptions.ConnectionClosedOK:
                            logger.info("OpenAI WebSocket connection closed normally")
                            break
                        except websockets.exceptions.ConnectionClosedError as e:
                            logger.error(f"OpenAI WebSocket connection closed with error: {e}")
                            break
                        except Exception as loop_err:
                            logger.error(f"Error processing message in send_to_twilio loop: {loop_err}")
                            continue
                except Exception as e:
                    logger.error("Error in send_to_twilio outer loop: %s", str(e))
                    try:
                        if openai_ws.open:
                            await openai_ws.close()
                        await websocket.close()
                    except Exception as close_err:
                        logger.error(f"Error closing websockets during outer exception handling: {close_err}")
                finally:
                    logger.info("send_to_twilio listening loop ended")
            
            async def offer_matching_slots(english_transcript: str, patient_details: dict, openai_ws):
                """Processes translated ENGLISH user transcript to find date and offer slots via AI."""
                logger.info("--- Starting offer_matching_slots ---")
                logger.info(f"Processing English transcript: '{english_transcript}'")
                
                english_months_pattern = 'January|February|March|April|May|June|July|August|September|October|November|December'
                date_patterns = [
                    rf'(\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{english_months_pattern})\s+\d{{4}})', 
                    rf'((?:{english_months_pattern})\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}})', 
                    r'(tomorrow)', 
                    r'(next\s+week)'
                ]
                
                extracted_date_str = None
                for pattern in date_patterns:
                    match = re.search(pattern, english_transcript, re.IGNORECASE)
                    if match:
                        extracted_date_str = match.group(1)
                        break
                
                if not extracted_date_str:
                    logger.info("No date pattern matched in user request")
                    await send_text(openai_ws, "I didn't catch a specific date, could you please repeat?")
                    logger.info("--- Ending offer_matching_slots (no date pattern match) ---")
                    return
                
                date_to_parse = extracted_date_str
                try:
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
                """Processes final AI transcript via LLM, finds slot, saves, and sends WhatsApp notification."""
                logger.info(f"Checking final AI transcript for booking confirmation: '{ai_transcript[:100]}...'")
                
                extracted_info = await translate_and_extract_appointment_info(ai_transcript)
                if not extracted_info:
                    logger.error("Translation/extraction of AI transcript failed")
                    return

                extracted_date = extracted_info.get("date")  # YYYY-MM-DD
                extracted_time = extracted_info.get("time")  # HH:MM:SS
                logger.info(f"LLM Extraction Result: Date={extracted_date}, Time={extracted_time}")

                if extracted_date and extracted_time:
                    logger.info("LLM extracted valid date and time. Attempting to find matching slot")
                    availability = patient_details.get("availability", [])
                    found_slot_id = None
                    for slot in availability:
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
                                
                                try:
                                    date_obj = datetime.strptime(extracted_date, "%Y-%m-%d")
                                    time_obj = datetime.strptime(extracted_time, "%H:%M:%S")
                                    formatted_date = date_obj.strftime("%m/%d")
                                    formatted_time = time_obj.strftime("%I:%M %p").lower().lstrip("0")
                                    
                                    message = twilio_client.messages.create(
                                        from_='whatsapp:+14155238886',
                                        content_sid='HXb5b62575e6e4ff6129ad7c8efe1f983e',
                                        content_variables=json.dumps({
                                            "1": formatted_date,
                                            "2": formatted_time
                                        }),
                                        to=f"whatsapp:{config['YOUR_PHONE_NUMBER']}"
                                    )
                                    logger.info(f"WhatsApp notification sent successfully. Message SID: {message.sid}")
                                except Exception as e:
                                    logger.error(f"Failed to send WhatsApp notification: {str(e)}")
                            else:
                                logger.error("Failed to save appointment (save_appointment returned False)")
                        except Exception as e:
                            logger.error(f"Error calling save_appointment: {str(e)}")
                    else:
                        logger.warning(f"LLM extracted date/time '{extracted_date} {extracted_time}', but no matching available slot found")
                else:
                    logger.info("LLM did not extract a confirmable date/time from this AI transcript")
            
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

            logger.info("Starting Twilio-OpenAI streaming for call")
            await asyncio.gather(receive_from_twilio(), send_to_twilio())
            logger.info("Streaming completed. Proceeding to call recording and transcription.")

    except WebSocketDisconnect:
        logger.info(f"Twilio WebSocket disconnected for stream_sid: {state.get('stream_sid')}, call_sid: {state.get('call_sid')}. Proceeding to final cleanup and transcription.")
    except Exception as e:
        logger.error(f"Media stream handler failed for call_sid {state.get('call_sid')}: {str(e)}", exc_info=True)
        # Optionally re-raise if you want the main FastAPI error handling to catch it
        # raise
    finally:
        logger.info(f"Entering finally block for call_sid: {state.get('call_sid')}. Cleaning up and attempting transcription.")
        # Ensure OpenAI WebSocket is closed if it was opened and is still open
        # This check needs to be more robust if openai_ws is not always defined in this scope
        # For now, assuming it might exist from the try block context if connection was successful.
        # A better pattern would be to initialize openai_ws = None before the try block.
        # However, the original structure has it inside the try-with-resources for websockets.connect.
        # Let's focus on the transcription logic placement first.

        call_sid_for_processing = state.get("call_sid")
        if call_sid_for_processing:
            logger.info(f"Post-call processing in finally: Downloading recording and transcribing for call SID {call_sid_for_processing}...")
            try:
                # Ensure twilio_client is accessible here or passed appropriately
                audio_path = await download_twilio_recording(call_sid_for_processing, twilio_client, config)
                
                if audio_path:
                    logger.info(f"Recording downloaded: {audio_path}")
                    # Ensure openai_api_client is accessible here
                    txt_path, json_path = await transcribe_audio_with_whisper(audio_path, call_sid_for_processing, openai_api_client)
                    logger.info(f"Transcript saved as {txt_path} and {json_path}")
                    
                    # Clean up the audio file
                    try:
                        await asyncio.to_thread(os.remove, audio_path)
                        logger.info(f"Deleted local audio file: {audio_path}")
                    except Exception as rm_err:
                        logger.error(f"Error cleaning up audio file {audio_path}: {rm_err}")
                else:
                    logger.error(f"Failed to download audio for call {call_sid_for_processing}. Skipping transcription.")
            except Exception as e:
                logger.error(f"Post-call recording/transcription in finally block failed for {call_sid_for_processing}: {e}", exc_info=True)
        else:
            logger.warning("No call_sid found in state within finally block. Cannot process recording for transcription.")
        
        # Ensure the Twilio WebSocket is closed if it's still open
        # This is tricky because the original error might be that it's *already* closed or not properly accepted.
        # A simple close attempt might also error. Consider adding a check for websocket.client_state.
        try:
            if websocket.client_state == WebSocketState.CONNECTED:
                logger.info(f"Attempting to close Twilio WebSocket in finally block for call_sid: {state.get('call_sid')}")
                await websocket.close()
                logger.info(f"Twilio WebSocket successfully closed in finally block for call_sid: {state.get('call_sid')}.")
            else:
                logger.info(f"Twilio WebSocket already in state {websocket.client_state.name} for call_sid: {state.get('call_sid')}. No explicit close needed here.")
        except RuntimeError as re:
            if "Cannot call \"send\" once a close message has been sent" in str(re) or \
               "WebSocket is not connected" in str(re): # Covering both common benign messages
                logger.info(f"Twilio WebSocket already closed or in an unsendable state for call_sid {state.get('call_sid')}: {str(re)}")
            else:
                # Log other RuntimeErrors as actual errors
                logger.error(f"RuntimeError closing Twilio WebSocket in finally block for call_sid {state.get('call_sid')}: {str(re)}", exc_info=True)
        except Exception as close_err:
            logger.error(f"Generic error closing Twilio WebSocket in finally block for call_sid {state.get('call_sid')}: {str(close_err)}", exc_info=True)

        logger.info(f"handle_media_stream finished for call_sid: {state.get('call_sid')}.")

# Helper functions for call recording and transcription

async def download_twilio_recording(call_sid, twilio_client_instance, app_config, max_wait_sec=120):
    """Polls Twilio for a recording for the given call SID, downloads WAV locally, returns filename."""
    logger.info(f"Polling Twilio for recording for call_sid: {call_sid}...")
    account_sid = app_config["TWILIO_ACCOUNT_SID"]
    auth_token = app_config["TWILIO_AUTH_TOKEN"]
    audio_filename = f"call_audio_{call_sid}.wav"

    for _ in range(int(max_wait_sec // 5)):
        try:
            recordings = twilio_client_instance.recordings.list(call_sid=call_sid, limit=1)
            if recordings:
                rec = recordings[0]
                # Ensure recording is in a final state if possible, though Twilio usually makes it available quickly.
                # rec.status like 'completed', 'processed' or similar could be checked if issues arise.
                
                recording_uri = f"/2010-04-01/Accounts/{account_sid}/Recordings/{rec.sid}.wav"
                full_url = f"https://api.twilio.com{recording_uri}"
                
                logger.info(f"Found recording SID: {rec.sid} for call {call_sid}. Attempting download from {full_url}")
                
                # Run synchronous requests call in a separate thread
                response_content = await asyncio.to_thread(
                    requests.get, full_url, auth=(account_sid, auth_token)
                )
                
                if response_content.status_code == 200:
                    with open(audio_filename, "wb") as f:
                        f.write(response_content.content)
                    logger.info(f"Recording for call {call_sid} downloaded to {audio_filename}")
                    return audio_filename
                else:
                    logger.error(f"Failed to download recording {rec.sid} for call {call_sid}. Status: {response_content.status_code}, Response: {response_content.text[:200]}")
            else:
                logger.info(f"No recording found yet for call {call_sid} on attempt. Waiting...")
        except Exception as e:
            logger.error(f"Error polling or downloading recording for {call_sid}: {e}", exc_info=True)

        await asyncio.sleep(5) # Wait 5 seconds before next poll
        
    logger.error(f"Recording not found or downloaded for call {call_sid} after {max_wait_sec} seconds.")
    return None

async def transcribe_audio_with_whisper(audio_path, call_sid, openai_client_instance):
    """Sends audio to OpenAI Whisper, saves transcript as .txt and .json."""
    if not audio_path or not openai_client_instance:
        logger.error(f"No audio path ({audio_path}) or OpenAI client provided for transcription for call {call_sid}.")
        return None, None

    logger.info(f"Transcribing audio file: {audio_path} for call {call_sid} using Whisper.")
    transcript_json_filename = f"transcript_{call_sid}.json"
    transcript_text_filename = f"transcript_{call_sid}.txt"

    try:
        with open(audio_path, "rb") as audio_file:
            # Run the synchronous OpenAI SDK call in a separate thread
            transcript_obj = await asyncio.to_thread(
                openai_client_instance.audio.transcriptions.create,
                model="whisper-1",
                file=audio_file,
                response_format="verbose_json" # Request verbose JSON for more details
            )
        
        # Extract text, robustly handling if transcript_obj is a dict or an object
        if hasattr(transcript_obj, 'text'):
            full_transcript_text = transcript_obj.text
        elif isinstance(transcript_obj, dict) and 'text' in transcript_obj:
            full_transcript_text = transcript_obj.get('text', "")
        else: # Fallback if structure is unexpected
            logger.warning(f"Unexpected transcript object structure for call {call_sid}. Trying to convert to string.")
            full_transcript_text = str(transcript_obj)

        logger.info(f"Whisper transcription successful for {call_sid}. Text length: {len(full_transcript_text)}")

        # Save as .txt
        with open(transcript_text_filename, "w", encoding="utf-8") as f:
            f.write(full_transcript_text)
        logger.info(f"Plain text transcript saved to: {transcript_text_filename}")

        # Save the full transcript object (verbose_json) as .json
        with open(transcript_json_filename, "w", encoding="utf-8") as f:
            # If transcript_obj is not a Pydantic model but a dict (older SDK versions might return dicts)
            if isinstance(transcript_obj, dict):
                 json.dump(transcript_obj, f, ensure_ascii=False, indent=2)
            else: # Assuming it's a Pydantic model with a model_dump method (newer SDK versions)
                 json.dump(transcript_obj.model_dump(), f, ensure_ascii=False, indent=2)
        logger.info(f"JSON transcript saved to: {transcript_json_filename}")
        return transcript_text_filename, transcript_json_filename

    except Exception as e:
        logger.error(f"Error during Whisper transcription for {audio_path} (call {call_sid}): {e}", exc_info=True)
        return None, None

async def trigger_call():
    logger.info("Using TWILIO_ACCOUNT_SID: %s", config["TWILIO_ACCOUNT_SID"])
    logger.info("Using TWILIO_AUTH_TOKEN: %s", config["TWILIO_AUTH_TOKEN"][:4] + "****")
    logger.info("Calling from %s to %s", config["TWILIO_PHONE_NUMBER"], config["YOUR_PHONE_NUMBER"])
    logger.info("Loaded NGROK_HOSTNAME: %s", config.get("NGROK_HOSTNAME", "Not Found - Check .env!"))
    # Ensure NGROK_HOSTNAME is correctly set in .env and points to your public URL (e.g., ngrok)
    # CRITICAL: Use the correct endpoint for outbound TwiML
    hostname = config.get("NGROK_HOSTNAME")
    if not hostname:
        logger.error("FATAL: NGROK_HOSTNAME environment variable not set or not loaded correctly from .env!")
        raise ValueError("NGROK_HOSTNAME is not configured. Cannot create call URL.")
        
    call_twiml_url = f"https://{hostname}/outbound-call-twiml"
    call = twilio_client.calls.create(
        url=call_twiml_url,
        to=config["YOUR_PHONE_NUMBER"],
        from_=config["TWILIO_PHONE_NUMBER"],
        record=True # Enable Twilio call recording
    )
    logger.info("Initiated outbound call (%s) pointing to TwiML URL: %s", call.sid, call_twiml_url)
    return {"status": "Call initiated!", "call_sid": call.sid}