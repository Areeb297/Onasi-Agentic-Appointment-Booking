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
                "You are a helpful AI receptionist working at Allballa Dental Center. "
                "Ignore any default greetings. Use the provided custom greeting exactly. "
                
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
    
    # Check availability and construct initial greeting
    available_slots_list = patient_details.get("availability", [])
    if not available_slots_list:
        availability_message = "I see that we currently have no open appointment slots."
    else:
        slots_to_mention = ', '.join([slot['display'] for slot in available_slots_list[:3]])
        availability_message = f"Our available slots currently include: {slots_to_mention}."

    initial_text = (
        f"Hello there {name}! This is AI Dental Assistant OnasiHelper calling from Allballa Dental Center. "
        f"Would you prefer to continue in English or Arabic? " # Language question
        f"{history_context}"
        f"I'm reaching out regarding {action}. "
        f"Your next follow-up appointment is due, and I'd like to schedule it for you. "
        f"{availability_message} Do you have a preferred date and time, or would you like to hear more options?"
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
