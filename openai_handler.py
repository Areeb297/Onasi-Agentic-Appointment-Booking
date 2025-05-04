"""
final_openai_handler.py
-----------------
Handles OpenAI Realtime API interactions for audio and text processing.
Fixed parameter order and integrated with robust database module.
"""

import json
from datetime import datetime, timezone
import logging
import re # Import re for validation
from database import get_patient_by_id
import os # Import os for API key
from openai import OpenAI # Import standard OpenAI client

logger = logging.getLogger(__name__)

# Initialize standard OpenAI client for translation
try:
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
except Exception as e:
    logger.error(f"Failed to initialize standard OpenAI client: {e}")
    client = None

async def translate_and_extract_appointment_info(text: str) -> dict | None:
    """
    Uses GPT-4o to translate text and extract confirmed appointment details.
    Returns: {"translation": str, "date": "YYYY-MM-DD"|None, "time": "HH:MM:SS"|None}
    """
    if not client:
        logger.error("OpenAI client not initialized. Cannot process text.")
        return None
    if not text.strip():
        logger.info("Input text for processing is empty.")
        return {"translation": "", "date": None, "time": None}

    logger.info(f"Attempting to translate/extract info from: '{text[:100]}...'")
    current_year = datetime.now().year
    prompt_messages = [
        {"role": "system", "content": f"""
You are an assistant processing call transcripts for appointment scheduling.
1. Translate the following user text to English.
2. Analyze the original text AND the English translation.
3. Determine if the text clearly confirms a specific appointment booking/scheduling (e.g., contains phrases like "I have scheduled", "booked for", "appointment is set for", "تم تحديد موعدك", "لقد حجزت موعدك", "لقد حددت موعدا لزيارتك"). Phrases merely stating availability (e.g., "is available", "would you like", "هل يناسبك") are NOT confirmations.
4. If a specific date and start time are clearly being confirmed:
    - Extract the date and format it strictly as YYYY-MM-DD. Assume the current year ({current_year}) if no year is explicitly mentioned.
    - Extract the start time and format it strictly as HH:MM:SS (24-hour format). Convert AM/PM if necessary (e.g., 2:00 PM becomes 14:00:00).
5. Respond ONLY with a valid JSON object containing exactly three keys:
    - "translation": The English translation of the original text (string).
    - "date": The extracted date in YYYY-MM-DD format (string), or null if no specific date was confirmed.
    - "time": The extracted start time in HH:MM:SS format (string), or null if no specific start time was confirmed.

Example Input: "لقد حجزت موعدك يوم 29 أبريل من الساعة 2:00 ظهرًا"
Example Output: {{"translation": "I have booked your appointment on April 29th from 2:00 PM.", "date": "{current_year}-04-29", "time": "14:00:00"}}

Example Input: "Okay, that works."
Example Output: {{"translation": "Okay, that works.", "date": null, "time": null}}

Example Input: "We have April 30th available at 3pm."
Example Output: {{"translation": "We have April 30th available at 3pm.", "date": null, "time": null}}
"""},
        {"role": "user", "content": text}
    ]

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=prompt_messages,
            temperature=0.3,
            response_format={"type": "json_object"} # Request JSON output
        )
        content = response.choices[0].message.content
        logger.debug(f"Raw JSON response from OpenAI extraction: {content}")
        extracted_info = json.loads(content)

        if not isinstance(extracted_info, dict) or not all(k in extracted_info for k in ["translation", "date", "time"]):
             logger.error(f"OpenAI extraction response lacks required keys: {content}")
             return {"translation": str(content), "date": None, "time": None}

        # Validate formats
        date_val = extracted_info.get("date")
        time_val = extracted_info.get("time")
        if date_val and not re.match(r"^\d{4}-\d{2}-\d{2}$", date_val):
            logger.warning(f"Extracted date '{date_val}' invalid format. Setting to None.")
            extracted_info["date"] = None
        if time_val and not re.match(r"^\d{2}:\d{2}:\d{2}$", time_val):
             logger.warning(f"Extracted time '{time_val}' invalid format. Setting to None.")
             extracted_info["time"] = None

        logger.info(f"Extraction result: Date='{extracted_info.get('date')}', Time='{extracted_info.get('time')}', Translation='{extracted_info.get('translation', '')[:50]}...'")
        return extracted_info

    except json.JSONDecodeError as json_err:
        logger.error(f"Failed to parse JSON extraction response: {json_err}. Content: {content}")
        return {"translation": "Extraction Error", "date": None, "time": None}
    except Exception as e:
        logger.error(f"Error during OpenAI extraction API call: {e}", exc_info=True)
        return None

async def initialize_openai_session_outbound(openai_ws, db_conn):
    """Initializes the OpenAI session specifically for outbound appointment scheduling calls."""
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.5,
                "prefix_padding_ms": 300,
                "silence_duration_ms": 600
            },
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": "alloy",
            "instructions": (
                "You are a helpful AI receptionist working at Allballa Dental Center making an **outbound call** to the patient. "
                "Your primary purpose is to schedule their follow-up appointment ({action}). "
                "Ignore any default greetings. Use the provided custom greeting exactly. "
                "**After asking 'Would you prefer to continue in English or Arabic?', pause naturally for a few seconds to allow the user to respond before continuing the greeting.** "
                
                "**CRITICAL RULES for Availability & Booking:**\n"
                "1. You have been provided with a list of currently available appointment slots. Refer **ONLY** to this list when discussing or offering appointments. Do not invent slots or dates.\n"
                "2. If the provided list of available slots is EMPTY, you **MUST** inform the user clearly that there are currently no openings and suggest calling back later. Do not offer to schedule anything.\n"
                "3. If the user asks for a date/time that is NOT on the provided availability list, you **MUST** state that the specific time is unavailable. Only suggest alternatives *if* there are other slots available on the provided list. If the list is empty, reiterate that nothing is available.\n"
                "4. **NEVER** use confirmation phrases ('I have scheduled...', 'Your appointment is confirmed...', 'Successfully booked...') unless you have first identified a specific, available slot **from the provided list** and the user has explicitly agreed to book that exact slot.\n" 
                "When you have successfully confirmed an available slot with the user and are about to finalize the booking, you MUST use **exactly one** of the following phrases and nothing else immediately after:\n" 
                "- 'I have scheduled your appointment for [DATE/TIME]'\n"
                "- 'Your appointment is confirmed for [DATE/TIME]'\n"
                "- 'Successfully booked for [DATE/TIME]'\n"
                "Replace [DATE/TIME] with the correct details. Do not add extra words before or after these specific phrases when confirming the final booking.\n" 
                "ALWAYS:\n"
                "1. State dates as 'March 30th, 2024 from 3:00 PM to 4:00 PM'\n"
                "2. Include both date and time ranges\n"
                "3. Wait for confirmation before ending call\n"
                "4. Listen carefully for the patient's preferred date and time\n"
                "5. Repeat back the date and time to confirm understanding\n"
                "6. Use the exact confirmation phrases when booking is successful\n"
                "Respond promptly to user speech with available slots or clarification."
            ),
            "modalities": ["text", "audio"],
            "temperature": 0.8
        }
    }
    logger.info("Sending session update: %s", json.dumps(session_update))
    print("Sending session update")
    await openai_ws.send(json.dumps(session_update))
    
    patient_id = 1
    patient_details = get_patient_by_id(conn=db_conn, patient_id=patient_id)
    if not patient_details:
        logger.error("Failed to retrieve patient details for ID %d", patient_id)
        raise ValueError(f"Patient ID {patient_id} not found")
    
    # Filter availability to include only future slots
    try:
        today_date = datetime.now().date()
        future_availability = []
        for slot in patient_details.get("availability", []) :
            slot_date = datetime.strptime(slot['date'], '%Y-%m-%d').date()
            if slot_date >= today_date:
                future_availability.append(slot)
        
        # Replace original availability with filtered list
        patient_details["availability"] = future_availability
        logger.info(f"Filtered future availability: {len(future_availability)} slots")
    except Exception as filter_err:
        logger.error(f"Error filtering availability: {filter_err}")
        # Continue with unfiltered list if filtering fails

    await send_initial_conversation_item(openai_ws, patient_details)
    return patient_details

async def initialize_openai_session_inbound(openai_ws):
    """Initializes the OpenAI session specifically for inbound/general inquiry calls."""
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": { 
                "type": "server_vad",
                "threshold": 0.5,
                "prefix_padding_ms": 300,
                "silence_duration_ms": 600
            },
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": "alloy",
            "instructions": (
                "You are AI Dental Assistant OnasiHelper for Allballa Dental Center handling INBOUND calls. "
                "**ULTRA-CRITICAL INSTRUCTIONS - FOLLOW THESE RULES PRECISELY AND WITHOUT FAIL:**\n"
                "1. **INITIAL GREETING ONLY:** Your VERY FIRST and ONLY initial action is to say **EXACTLY** this phrase once: 'Thank you for calling Alballa Dental Center! This is AI Dental Assistant OnasiHelper. How can I help you today?' \n"
                "2. **ABSOLUTE SILENCE AFTER GREETING:** After saying the initial greeting (Step 1), you **MUST STOP SPEAKING**. Remain completely silent. Do NOT say anything else. Your microphone is effectively off until the caller speaks.\n"
                "3. **REACT ONLY TO CALLER SPEECH:** Your ONLY trigger to speak again after the initial greeting is detecting actual speech from the caller. Do NOT react to silence, background noise, or internal timers.\n"
                "4. **RESPONSE TO VAGUE GREETINGS:** If the caller's first speech is a simple greeting like 'hello', 'hi', or similar, respond **ONCE** with **EXACTLY**: 'Hello! You have reached AlBalla Dental Center, how can I help you today?' Then STOP SPEAKING and wait for their specific request.\n"
                "5. **NO REPEATED INTRODUCTIONS:** **NEVER** repeat your name or role ('OnasiHelper', 'AI Dental Assistant') after the initial greeting (Step 1) unless the caller explicitly asks 'Who am I speaking with?' or similar. Do NOT say 'again'.\n"
                "6. **HANDLE SPECIFIC QUERIES:** If the caller asks a specific question, answer it concisely and directly. Then STOP SPEAKING and wait.\n"
                "7. **NO UNPROMPTED TALKING:** Do not fill silence. Do not make assumptions. Do not offer information not asked for. Do not ask follow-up questions unless necessary to clarify their request.\n"
                "8. **NO PREMATURE GOODBYE:** Do not say goodbye unless the caller says goodbye first.\n"
            ),
            "modalities": ["text", "audio"],
            "temperature": 0.7 # Lowered temperature
        }
    }
    logger.info("Sending session update for INBOUND call: %s", json.dumps(session_update))
    print("Sending INBOUND session update")
    await openai_ws.send(json.dumps(session_update))

    # Send the initial greeting for inbound calls
    greeting_message = {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "Thank you for calling Alballa Dental Center! This is AI Dental Assistant OnasiHelper. How can I help you today?"}]
        }
    }
    await openai_ws.send(json.dumps(greeting_message))
    logger.info("Sent inbound initial greeting message")
    print("Inbound initial greeting message sent")

    # Now, just wait for actual user input after sending the greeting.
    logger.info("Waiting for user speech input after greeting.")
    print("Waiting for user speech input after greeting.")

    # No patient details needed/returned for this simple inbound case yet

async def send_initial_conversation_item(openai_ws, patient_details):
    current_date_obj = datetime.now()
    current_date_str = current_date_obj.strftime("%B %d, %Y")
    current_time_str = current_date_obj.astimezone().strftime("%I:%M %p %Z")
    
    # Use the potentially pre-filtered availability list
    formatted_availability = "\n".join(
        [f"- {slot['display']}" for slot in patient_details["availability"]]
    )
    if not patient_details["availability"]:
         formatted_availability = "None"

    # Log the formatted_availability for debugging
    logger.info(f"Formatted availability: {formatted_availability}")

    # Update system message instructions
    system_message_text = (
        "You are a helpful AI receptionist working at Allballa Dental Center. "
        "Please ignore any default greetings and use the following style when greeting the caller: "
        "'Hello there {name}! This is AI Dental Assistant OnasiHelper calling from Allballa Dental Center. "
        "Would you prefer to continue in English or Arabic? "
        "{history_context}I'm reaching out regarding {action}. Your next follow-up appointment is due, "
        "and I'd like to schedule it for you. Do you have a preferred date and time?'. "
        "Always follow the center's protocols and provide accurate scheduling information."
        f"Today's date is {current_date_str}. The list below contains **only future** available appointment slots relative to today:\n{formatted_availability}\n" # Use the correctly formatted string here
        "If the list is empty (shows 'None'), you MUST inform the user no slots are available.\n"
        "When asked about dates like 'next week', calculate relative to today ({current_date_str}) and check against the future slots provided.\n"
        "1. Acknowledge the patient's preference\n"
        "2. Check availability against clinic schedule\n"
        "3. If available, confirm with exact date/time using 'I have scheduled your appointment for [DATE/TIME]'\n"
        "4. If unavailable, suggest nearest options\n"
        "5. Always verify patient acceptance\n"
        "6. Listen carefully for date/time mentions in patient speech\n"
        "7. When a patient mentions a date, always respond with confirmation of that date\n"
        "8. Use the exact booking confirmation phrases when an appointment is confirmed\n"
    )
    
    name = patient_details.get("name", "there")
    action = patient_details.get("action", "your appointment")
    medical_history = patient_details.get("medical_history", "")
    history_context = f"I see from your records that your medical history includes {medical_history}. " if medical_history else ""
    
    system_message_text = system_message_text.format(
        name=name, 
        history_context=history_context, 
        action=action,
        formatted_availability=formatted_availability, # Pass the variable here
        current_date_str=current_date_str, # Pass string version
        current_time=current_time_str # Pass string version
    )

    system_message_item = {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "system",
            "content": [{"type": "text", "text": system_message_text}]
        }
    }
    await openai_ws.send(json.dumps(system_message_item))
    logger.info("Sent system message with updated instructions")
    print("System message sent")
    
    # Construct and send the FULL initial greeting text directly
    initial_text = (
        f"Hello there {name}! This is AI Dental Assistant OnasiHelper calling from Allballa Dental Center. "
        f"Would you prefer to continue in English or Arabic? "
        f"{history_context}"
        f"I'm reaching out regarding {action}. "
        f"Your next follow-up appointment is due, and I'd like to schedule it for you. "
        f"Today's date is {current_date_str}. The list below contains **only future** available appointment slots relative to today:\n{formatted_availability}\n" # Corrected: Use single braces
        f"If the list is empty (shows 'None'), you MUST inform the user no slots are available.\n"
        f"When asked about dates like 'next week', calculate relative to today ({current_date_str}) and check against the future slots provided.\n"
        f"1. Acknowledge the patient's preference\n"
        f"2. Check availability against clinic schedule\n"
        f"3. If available, confirm with exact date/time using 'I have scheduled your appointment for [DATE/TIME]'\n"
        f"4. If unavailable, suggest nearest options\n"
        f"5. Always verify patient acceptance\n"
        f"6. Listen carefully for date/time mentions in patient speech\n"
        f"7. When a patient mentions a date, always respond with confirmation of that date\n"
        f"8. Use the exact booking confirmation phrases when an appointment is confirmed\n"
    )

    # Log the initial_text for debugging (truncated)
    logger.info(f"Initial greeting text (first 500 chars): {initial_text[:500]}...")

    initial_conversation_item = {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": initial_text}]
        }
    }
    await openai_ws.send(json.dumps(initial_conversation_item))
    logger.info("Sent initial greeting message content with correct availability")
    print("Initial greeting message content sent")
    
    # Ensure response.create is NOT commented out
    await openai_ws.send(json.dumps({"type": "response.create"})) 
    logger.info("Triggered initial response")
    print("Response triggered")
