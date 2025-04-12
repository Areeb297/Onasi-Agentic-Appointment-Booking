"""
final_openai_handler.py
-----------------
Handles OpenAI Realtime API interactions for audio and text processing.
Fixed parameter order and integrated with robust database module.
"""

import json
from datetime import datetime, timezone
import logging
from database import get_patient_by_id

logger = logging.getLogger(__name__)

async def initialize_openai_session(openai_ws, db_conn):
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {"type": "server_vad", "threshold": 0.5, "prefix_padding_ms": 300, "silence_duration_ms": 500},
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": "alloy",
            "instructions": (
                "You are a helpful AI receptionist working at Allballa Dental Center. "
                "Ignore any default greetings. Use the provided custom greeting exactly. "
                "When confirming appointments, **always** use one of these exact phrases:\n"
                "- 'I have scheduled your appointment for [DATE/TIME]'\n"
                "- 'Your appointment is confirmed for [DATE/TIME]'\n"
                "- 'Successfully booked for [DATE/TIME]'\n"
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
    # FIXED: Corrected parameter order - conn first, then patient_id
    # This fixes the "'int' object has no attribute 'cursor'" error
    patient_details = get_patient_by_id(conn=db_conn, patient_id=patient_id)
    if not patient_details:
        logger.error("Failed to retrieve patient details for ID %d", patient_id)
        raise ValueError(f"Patient ID {patient_id} not found")
    
    await send_initial_conversation_item(openai_ws, patient_details)
    return patient_details

async def send_initial_conversation_item(openai_ws, patient_details):
    current_date = datetime.now(timezone.utc).astimezone().strftime("%B %d, %Y")
    current_time = datetime.now(timezone.utc).astimezone().strftime("%I:%M %p %Z")
    
    formatted_availability = "\n".join(
        [f"- {slot['display']}" for slot in patient_details["availability"]]
    )
    
    system_message_text = (
        "You are a helpful AI receptionist working at Allballa Dental Center. "
        "Please ignore any default greetings and use the following style when greeting the caller: "
        "'Hello there {name}! This is AI Dental Assistant OnasiHelper calling from Allballa Dental Center. "
        "{history_context}I'm reaching out regarding {action}. Your next follow-up appointment is due, "
        "and I'd like to schedule it for you. Do you have a preferred date and time?'. "
        "Always follow the center's protocols and provide accurate scheduling information."
        f"Current appointment availabilities include:\n{formatted_availability}\n"
        "If not available, suggest the nearest options. Always verify patient acceptance. "
        f"Today's date is {current_date} and current time is {current_time}. "
        "When discussing appointments:\n"
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
        name=name, history_context=history_context, action=action
    )
    
    system_message_item = {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "system",
            "content": [{"type": "input_text", "text": system_message_text}]
        }
    }
    await openai_ws.send(json.dumps(system_message_item))
    logger.info("Sent system message")
    print("System message sent")
    
    initial_text = (
        f"Hello there {name}! This is AI Dental Assistant OnasiHelper calling from Allballa Dental Center. "
        f"{history_context}"
        f"I'm reaching out regarding {action}. "
        f"Your next follow-up appointment is due, and I'd like to schedule it for you. "
        f"Do you have a preferred date and time? Our available slots are: {', '.join([slot['display'] for slot in patient_details['availability'][:3]])}"
    )
    
    initial_conversation_item = {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "input_text", "text": initial_text}]
        }
    }
    await openai_ws.send(json.dumps(initial_conversation_item))
    logger.info("Sent initial greeting")
    print("Initial greeting sent")
    
    await openai_ws.send(json.dumps({"type": "response.create"}))
    logger.info("Triggered initial response")
    print("Response triggered")
