from fastapi import Request, WebSocket
from fastapi.responses import HTMLResponse
from twilio.twiml.voice_response import VoiceResponse, Connect
import logging
import asyncio
from fastapi import WebSocketDisconnect
import websockets
import json
import base64
from config import load_clean_config
# Import the specific inbound initializer and potentially common elements if needed later
from openai_handler import initialize_openai_session_inbound

logger = logging.getLogger(__name__)
config = load_clean_config()

# Define typical AI closing phrases (lowercase) - Reuse from outbound or define specific ones
AI_GOODBYE_PHRASES_INBOUND = [
    "have a great day",
    "thanks for calling",
    "goodbye",
    "take care"
]

async def handle_incoming_call(request: Request):
    """
    Handles incoming calls to your Twilio number.
    Responds with TwiML that starts a stream to /media-stream-inbound.
    """
    logger.info("Received inbound call from %s", request.client.host)
    response = VoiceResponse()
    host = request.url.hostname
    # Ensure correct WebSocket protocol (wss:// for secure connections)
    ws_protocol = "wss://" if request.url.scheme in ['https', 'wss'] else "ws://"
    stream_url = f"{ws_protocol}{host}/media-stream-inbound"
    connect = Connect()
    connect.stream(url=stream_url)
    response.append(connect)
    logger.info(f"Returning inbound TwiML to Twilio, streaming to: {stream_url}")
    # Return TwiML as XML
    return HTMLResponse(content=str(response), media_type="application/xml")

async def handle_media_stream_inbound(websocket: WebSocket):
    """
    Handles the WebSocket audio stream for inbound calls.
    """
    # CRITICAL: Must accept the WebSocket connection first
    await websocket.accept()
    logger.info("Inbound WebSocket accepted from %s", websocket.client)
    
    # Brief delay to ensure connection is fully established
    await asyncio.sleep(0.1)
    
    state = {
        "stream_sid": None,
        "latest_media_timestamp": 0,
        "last_assistant_item": None, # For potential interruption handling
        "mark_queue": [],
        "response_start_timestamp_twilio": None,
        "accumulated_text": "", # For full AI transcript
        "audio_chunk_count": 0,
        "transcript_buffer": "", # Buffer for AI transcript deltas
        "user_transcript": "",  # Store user's speech transcript
    }

    openai_ws = None
    
    try:
        openai_ws = await websockets.connect(
            'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01',
            extra_headers={
                "Authorization": f"Bearer {config['OPENAI_API_KEY']}",
                "OpenAI-Beta": "realtime=v1"
            }
        )
        
        logger.info("Inbound Handler: Connected to OpenAI WebSocket")
        await initialize_openai_session_inbound(openai_ws)
        logger.info("Inbound Handler: OpenAI session initialized and greeting sent.")

        # --- Audio Relay Coroutines (Simplified for Inbound) ---
        async def receive_from_twilio():
            nonlocal state
            logger.info("Inbound Handler: Starting receive_from_twilio")
            try:
                async for message in websocket.iter_text():
                    data = json.loads(message)

                    if data["event"] == "media" and openai_ws.open:
                        state["latest_media_timestamp"] = int(data["media"]["timestamp"])
                        audio_payload = data["media"]["payload"]
                        if not openai_ws.open:
                            continue

                        audio_append = {"type": "input_audio_buffer.append", "audio": audio_payload}
                        await openai_ws.send(json.dumps(audio_append))
                        state["audio_chunk_count"] += 1
                        # Minimal logging for audio forwarding - COMMENTED OUT
                        # if state["audio_chunk_count"] % 50 == 0:
                        #     logger.debug("Inbound: Forwarded audio chunk %d", state["audio_chunk_count"])

                    elif data["event"] == "start":
                        state["stream_sid"] = data["start"]["streamSid"]
                        logger.info("Inbound Handler: Twilio stream started: %s", state["stream_sid"])
                        # Send 20ms of silence (160 bytes of 0x7F for G711 μ-law, base64 encoded)
                        # COMMENTING OUT silence packet logging as it's routine
                        # silence_bytes = bytes([0x7F] * 160)
                        # silence_b64 = base64.b64encode(silence_bytes).decode("utf-8")
                        # await websocket.send_json({
                        #     "event": "media",
                        #     "streamSid": state["stream_sid"],
                        #     "media": {"payload": silence_b64}
                        # })
                        # logger.info("Inbound Handler: Sent initial silence packet to Twilio.")

                    elif data["event"] == "mark":
                        if state["mark_queue"]:
                            state["mark_queue"].pop(0)
                            # logger.debug("Inbound Handler: Processed mark event") # COMMENTED OUT

                    elif data["event"] == "stop":
                        logger.info("Inbound Handler: Twilio stream stopped.")
                        return # Exit the function

            except WebSocketDisconnect:
                logger.info("Inbound Handler: Twilio WebSocket disconnected.")
                if openai_ws.open:
                    await openai_ws.close()
                    logger.info("Inbound Handler: Closed OpenAI WebSocket due to Twilio disconnect.")
            except Exception as e:
                logger.error("Inbound Handler: Error in receive_from_twilio: %s", str(e), exc_info=True)
                if openai_ws.open: await openai_ws.close()
                raise
            finally:
                logger.info("Inbound Handler: receive_from_twilio loop ended")

        async def send_to_twilio():
            nonlocal state
            logger.info("Inbound Handler: Starting send_to_twilio listening loop.")
            try:
                while openai_ws.open:
                    try:
                        message_json = await asyncio.wait_for(openai_ws.recv(), timeout=60.0)
                        # logger.info("RAW OpenAI message: %s", message_json) # COMMENTED OUT - too verbose for normal operation
                        response = json.loads(message_json)
                        logger.debug("Inbound: Received OpenAI message: %s", json.dumps(response))

                        # Handle user speech transcript delta - COMMENTED OUT
                        # if response.get("type") == "input_audio_buffer.transcript.delta":
                        #     user_speech = response.get("transcript", "")
                        #     state["user_transcript"] += user_speech
                        #     # logger.debug("Inbound User speech delta: %s", user_speech) # Optional debug
                        
                        # Added else if to prevent double processing if transcript delta is handled above
                        if response.get("type") == "input_audio_buffer.transcript.delta":
                            user_speech = response.get("transcript", "")
                            state["user_transcript"] += user_speech # Still accumulate for final log
                            logger.debug("Inbound User speech delta: %s", user_speech)
                        elif response.get("type") == "input_audio_buffer.transcript.done":
                            final_transcript = state["user_transcript"].strip().lower()
                            logger.info(f"Inbound User transcript done: '{final_transcript}'")
                            
                            # Check for user-initiated goodbye first
                            triggered_goodbye = False
                            if final_transcript: # Only check if there is a transcript
                                for phrase in AI_GOODBYE_PHRASES_INBOUND:
                                    if phrase in final_transcript:
                                        logger.info(f"Inbound: Detected USER goodbye phrase: '{phrase}'. Triggering call end.")
                                        await send_text(openai_ws, "Thank you for calling! Goodbye.")
                                        await asyncio.sleep(0.5) 
                                        await openai_ws.close()
                                        triggered_goodbye = True
                                        break # Exit goodbye phrase check loop
                            
                            if triggered_goodbye:
                                state["user_transcript"] = "" # Clear buffer
                                return # Exit send_to_twilio loop

                            # Handle vague greetings (if not a goodbye)
                            if final_transcript in ["hello", "hi", "hey"]:
                                # Send a prompt that allows OpenAI to use Instruction #4
                                logger.info("Detected vague greeting. Sending 'User offered a greeting.' prompt to OpenAI.")
                                await send_text(openai_ws, "User offered a greeting.")
                                # Intentionally *not* logging the expected AI response here, 
                                # as OpenAI generates it based on the prompt + instructions.
                            
                            # Handle empty transcript (silence)
                            elif not final_transcript:
                                logger.info("User transcript was empty (silence). No action taken, waiting for user speech as per instructions.")
                                # We explicitly told the AI to wait silently in instructions, so we don't send anything here.
                                # No response.create should be triggered.
                            
                            # Handle specific non-goodbye input
                            elif final_transcript: 
                                logger.info("Processing specific user input: '%s'. Allowing OpenAI to respond based on context.", final_transcript)
                                # No specific action/prompt needed here; OpenAI's context awareness should handle it.
                                # A response.create might be triggered implicitly by OpenAI if it decides to respond.

                            state["user_transcript"] = "" # Reset buffer after processing

                        # Handle AI audio response
                        elif response.get("type") == "response.audio.delta" and "delta" in response:
                            audio_payload = base64.b64encode(base64.b64decode(response["delta"])).decode("utf-8")
                            await websocket.send_json({"event": "media", "streamSid": state["stream_sid"], "media": {"payload": audio_payload}})
                            if state["response_start_timestamp_twilio"] is None:
                                state["response_start_timestamp_twilio"] = state["latest_media_timestamp"]
                            if response.get("item_id"):
                                state["last_assistant_item"] = response["item_id"]
                            await send_mark(websocket, state["stream_sid"], state)

                        # Handle AI text transcript delta - COMMENTED OUT
                        # elif response.get("type") == "response.audio_transcript.delta":
                        #     state["transcript_buffer"] += response.get("transcript", "")
                        
                        # Added else if to prevent double processing if transcript delta is handled above
                        elif response.get("type") == "response.audio_transcript.delta":
                             state["transcript_buffer"] += response.get("transcript", "") # Still accumulate for final log
                        # Process complete AI transcript
                        elif response.get("type") == "response.audio_transcript.done":
                            state["accumulated_text"] = response.get("transcript", state["transcript_buffer"])
                            logger.info("Inbound AI full transcript: %s", state["accumulated_text"])
                            state["transcript_buffer"] = "" # Reset buffer
                            # No appointment confirmation check needed here for basic inbound

                        # Handle AI response completion & check for hangup
                        elif response.get("type") == "response.done":
                            full_text = state["accumulated_text"].lower()
                            logger.info("Inbound AI response text finalized: %s", full_text)
                            # triggered_hangup = False
                            # for phrase in AI_GOODBYE_PHRASES_INBOUND:
                            #     if phrase in full_text:
                            #         logger.info(f"Inbound: Detected AI goodbye phrase: '{phrase}'. Waiting 5s before triggering call end.")
                            #         await asyncio.sleep(5)
                            #         try:
                            #             if openai_ws.open: 
                            #                 await openai_ws.close()
                            #             # DON'T close the Twilio websocket here - let the main function handle it
                            #             # await websocket.close()
                            #             triggered_hangup = True
                            #             break
                            #         except Exception as close_err:
                            #             logger.error(f"Inbound: Error closing OpenAI websocket: {close_err}")
                            # if triggered_hangup:
                            #     logger.info("Inbound hangup triggered, breaking loop.")
                            #     break
                            # state["accumulated_text"] = "" # Reset if not hanging up
                            # Do NOT trigger hangup on AI-generated goodbye phrases
                            state["accumulated_text"] = ""

                        # Handle user speech detection (for potential interruption) - COMMENTED OUT info log
                        elif response.get("type") == "input_audio_buffer.speech_started":
                            # logger.info("Inbound: User speech detected at timestamp %d", state["latest_media_timestamp"])
                            # Basic interruption handling (optional for simple inbound)
                            # if state["last_assistant_item"]:
                            #     await handle_interruption(openai_ws, websocket, state)
                            logger.debug("Inbound: User speech detected at timestamp %d", state["latest_media_timestamp"])
                            pass # Keep the check but don't log unless needed

                        # Handle user speech completion (logging done in transcript.done) - COMMENTED OUT info log
                        elif response.get("type") == "input_audio_buffer.speech_finished":
                            # logger.info("Inbound: User speech finished.")
                            logger.debug("Inbound: User speech finished.")
                            pass # Keep the check but don't log unless needed

                    except asyncio.TimeoutError:
                        logger.debug("Inbound: OpenAI receive timeout.") # Keep debug for timeouts
                        if not openai_ws.open: break
                        continue
                    except websockets.exceptions.ConnectionClosedOK:
                        logger.info("Inbound: OpenAI WebSocket connection closed normally.")
                        break
                    except websockets.exceptions.ConnectionClosedError as e:
                        logger.error(f"Inbound: OpenAI WebSocket closed with error: {e}")
                        break
                    except Exception as loop_err:
                        logger.error(f"Inbound: Error processing message in send_to_twilio loop: {loop_err}")
                        continue

            except Exception as e:
                logger.error("Inbound: Error in send_to_twilio outer loop: %s", str(e))
            finally:
                logger.info("Inbound Handler: send_to_twilio listening loop ended.")
                # Close only the OpenAI websocket, not the Twilio one
                # try:
                #     if openai_ws.open: 
                #         await openai_ws.close()
                # except Exception:
                #     pass # Ignore errors if already closed
                try:
                    if openai_ws.open:
                        await openai_ws.close()
                    if state["stream_sid"]:
                        await websocket.send_json({"event": "clear", "streamSid": state["stream_sid"]})
                        logger.info("Inbound Handler: Sent clear event to Twilio.")
                    # Let the main handler close the Twilio websocket
                    # await websocket.close()
                except Exception as final_close_err:
                    logger.error(f"Inbound Handler: Error during final cleanup in send_to_twilio: {final_close_err}")

        # --- Helper functions (send_mark, optional handle_interruption) ---
        async def send_mark(connection, stream_sid, state):
            # Needs state for mark_queue
            if stream_sid:
                mark_event = {
                    "event": "mark",
                    "streamSid": stream_sid,
                    "mark": {"name": "responsePart"}
                }
                await connection.send_json(mark_event)
                state["mark_queue"].append("responsePart")
                # logger.debug("Inbound: Sent mark event") # COMMENTED OUT

        # Optional interruption handler (can be added later if needed)
        # async def handle_interruption(openai_ws, twilio_ws, state):
        #     ...
        
        # Helper function to send text messages to OpenAI
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
            logger.info(f"Sent text to OpenAI: '{text}'")
            await openai_ws.send(json.dumps({"type": "response.create"}))
            logger.info("Triggered response.create after sending text.")

        # --- Run the loops --- 
        logger.info("Inbound Handler: Starting Twilio-OpenAI streaming loops.")
        # Use wait=False to allow us to handle exceptions properly
        tasks = asyncio.gather(receive_from_twilio(), send_to_twilio(), return_exceptions=True)
        
        try:
            results = await tasks
            # Check if any task returned an exception
            for result in results:
                if isinstance(result, Exception):
                    logger.error(f"Inbound Handler: Task returned exception: {result}")
                    raise result
            logger.info("Inbound Handler: Streaming completed successfully.")
        except asyncio.CancelledError:
            logger.info("Inbound Handler: Tasks were cancelled.")
        except Exception as e:
            logger.error(f"Inbound Handler: Error during streaming: {e}")
            raise

    except WebSocketDisconnect:
        logger.info("Inbound WebSocket disconnected before OpenAI connection or during relay.")
    except Exception as e:
        logger.error(f"Error in inbound media stream handler: {e}", exc_info=True)
    finally:
        # Close OpenAI websocket if it's still open
        if openai_ws and openai_ws.open:
            try:
                await openai_ws.close()
                logger.info("Inbound Handler: Closed OpenAI WebSocket in finally block.")
            except Exception as e:
                logger.error(f"Inbound Handler: Error closing OpenAI WebSocket: {e}")
        
        # Close Twilio websocket if it's still open
        try:
            await websocket.close()
            logger.info("Inbound Handler: Closed Twilio WebSocket in finally block.")
        except Exception as e:
            logger.error(f"Inbound Handler: Error closing Twilio WebSocket: {e}")
            
        logger.info("Inbound media stream handler finished.") 