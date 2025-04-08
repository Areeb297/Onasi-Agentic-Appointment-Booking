# Standard libraries
import os                  # For accessing environment variables and file operations
import json                # For parsing and creating JSON data
import base64              # For encoding/decoding binary data
import asyncio             # For asynchronous programming
import websockets          # For WebSocket client functionality

# FastAPI for handling HTTP and WebSocket routes
from fastapi import FastAPI, WebSocket, Request           # Core FastAPI components
from fastapi.responses import HTMLResponse # type: ignore # For returning HTML content
from fastapi.websockets import WebSocketDisconnect        # Exception for WebSocket disconnections
from twilio.rest import Client
# Twilio SDK for building TwiML responses
from twilio.twiml.voice_response import VoiceResponse, Connect, Say, Stream  # Twilio voice response components

# .env loader for environment variables
from dotenv import load_dotenv  # For loading environment variables from .env file

# Logging for easier debugging
import logging  # Standard logging module

# === Logging Configuration ===
# Set up basic logging to display timestamps, log levels, and messages
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)  # Get a logger instance for this module

# === Load .env Variables ===
load_dotenv()  # Load environment variables from .env file

# Get OpenAI API key from environment
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')  # Required for authenticating with OpenAI API

# Port to run the FastAPI app
PORT = int(os.getenv('PORT', 5050))  # Default to port 5050 if not specified in environment

# === OpenAI Session Setup ===
# System message defines the AI's behavior and personality
SYSTEM_MESSAGE = (
  "You are a helpful AI receptionist working at Allballa Dental Center."
)

VOICE = 'alloy'  # Set OpenAI voice model (other options include echo, nova, shimmer)

# List of OpenAI event types to log for monitoring and debugging
LOG_EVENT_TYPES = [
  'response.content.done',               # Fired when content generation is complete
  'rate_limits.updated',                 # Fired when rate limit information is updated
  'response.done',                       # Fired when the entire response is complete
  'input_audio_buffer.committed',        # Fired when user audio is committed
  'input_audio_buffer.speech_stopped',   # Fired when user stops speaking
  'input_audio_buffer.speech_started',   # Fired when user starts speaking
  'response.create',                     # Fired when a new response is created
  'session.created'                      # Fired when a new session is created
]

SHOW_TIMING_MATH = False  # Toggle for debugging response timing calculations

# === FastAPI App ===
app = FastAPI()  # Initialize the FastAPI application

# Safety check to ensure API key is loaded
if not OPENAI_API_KEY:
  raise ValueError('Missing the OpenAI API key. Please set it in the .env file.')

# Import database connector and typing for type hints
import pyodbc                            # For SQL Server connection
from typing import Optional, Dict, List  # Type hints for function signatures

# SQL Server Connection Configuration
# SQL Server Connection Settings from environment variables
SQL_SERVER_DRIVER = os.getenv('SQL_SERVER_DRIVER')                  # ODBC driver for SQL Server
SQL_SERVER_SERVER = os.getenv('SQL_SERVER_SERVER')                  # SQL Server instance name
SQL_SERVER_DATABASE = os.getenv('SQL_SERVER_DATABASE')              # Database name
SQL_SERVER_TRUSTED_CONNECTION = os.getenv('SQL_SERVER_TRUSTED_CONNECTION')  # Windows Authentication flag


# --------------------------
# Database Connection
# --------------------------
def get_db_connection():
    """
    Create and return a connection to SQL Server using Windows Authentication.
    
    Returns:
        pyodbc.Connection: An active connection to the SQL Server database
        
    Raises:
        pyodbc.Error: If database connection fails due to connection issues
        Exception: For any other unexpected errors
    """
    try:
        # Build connection string from environment variables
        connection_string = (
            f'DRIVER={SQL_SERVER_DRIVER};'
            f'SERVER={SQL_SERVER_SERVER};'
            f'DATABASE={SQL_SERVER_DATABASE};'
            f'Trusted_Connection={SQL_SERVER_TRUSTED_CONNECTION}'
        )
        # Establish connection
        conn = pyodbc.connect(connection_string)
        logger.info(f"Successfully connected to database: {SQL_SERVER_DATABASE}")
        return conn
    except pyodbc.Error as e:
        # Log specific database connection errors
        logger.error(f"Database connection failed: {str(e)}")
        raise
    except Exception as e:
        # Log any other unexpected errors
        logger.error(f"Unexpected error in database connection: {str(e)}")
        raise


# Root route - confirms the server is running
@app.get("/", response_class=HTMLResponse)
async def index_page():
    """
    Simple health check endpoint that confirms the server is running.
    Returns HTML response that can be viewed in a browser.
    """
    return "<html><body><h1>Twilio Media Stream Server is running!</h1></body></html>"

# This route handles incoming Twilio calls
@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """
    Responds with TwiML instructing Twilio to start a Media Stream session.
    
    This endpoint is called by Twilio when a call comes in. It returns
    TwiML that tells Twilio to connect to our WebSocket endpoint.
    
    Args:
        request (Request): The incoming HTTP request from Twilio
        
    Returns:
        HTMLResponse: TwiML instructions for Twilio
    """
    logger.info("Received incoming call request from: %s", request.client.host)
    
    # Create a new TwiML response object
    response = VoiceResponse()
    
    # Get the hostname for creating the WebSocket URL
    host = request.url.hostname
    
    # Create a Connect verb to establish a media stream
    connect = Connect()
    
    # Tell Twilio to open a WebSocket to our media-stream endpoint
    connect.stream(url=f'wss://{host}/media-stream')
    
    # Add the Connect verb to the response
    response.append(connect)
    
    logger.info("Successfully created the TwiML response")
    return HTMLResponse(content=str(response), media_type="application/xml")

# This WebSocket route handles audio data streaming between Twilio and OpenAI
@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    """
    Main WebSocket handler that bridges Twilio and OpenAI.
    
    This function:
    1. Accepts the WebSocket connection from Twilio
    2. Establishes a connection to OpenAI's realtime API
    3. Forwards audio between Twilio and OpenAI
    4. Handles interruptions and conversation flow
    
    Args:
        websocket (WebSocket): The WebSocket connection from Twilio
    """
    print("Client connected")
    await websocket.accept()  # Accept the WebSocket connection from Twilio

    # Open WebSocket connection to OpenAI's realtime API with authentication
    async with websockets.connect(
        'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01',
        extra_headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "OpenAI-Beta": "realtime=v1"
        }
    ) as openai_ws:
        # Send initial session settings to OpenAI and start the conversation
        await send_session_update(openai_ws)

        # Local state variables for managing the conversation
        stream_sid = None                      # Twilio's unique stream identifier
        latest_media_timestamp = 0             # Track the timing of audio packets
        last_assistant_item = None             # Track the ID of the AI's responses
        mark_queue = []                        # Queue for synchronization markers
        response_start_timestamp_twilio = None # Track when AI responses start

        # === TWILIO TO OPENAI ===
        async def receive_from_twilio():
            """
            Reads incoming audio from Twilio and forwards it to OpenAI.
            
            This function continuously:
            1. Receives WebSocket messages from Twilio
            2. Processes different event types (media, start, mark, stop)
            3. Forwards audio data to OpenAI
            """
            nonlocal stream_sid, latest_media_timestamp
            try:
                async for message in websocket.iter_text():
                    data = json.loads(message)
                    
                    if data['event'] == 'media' and openai_ws.open:
                        # Received audio data from Twilio - forward to OpenAI
                        latest_media_timestamp = int(data['media']['timestamp'])
                        audio_append = {
                            "type": "input_audio_buffer.append",
                            "audio": data['media']['payload']  # Base64-encoded audio
                        }
                        await openai_ws.send(json.dumps(audio_append))
                        
                    elif data['event'] == 'start':
                        # Twilio stream started - initialize conversation state
                        stream_sid = data['start']['streamSid']
                        print(f"Incoming stream has started {stream_sid}")
                        response_start_timestamp_twilio = None
                        latest_media_timestamp = 0
                        last_assistant_item = None
                        
                    elif data['event'] == 'mark':
                        # Process mark events for message synchronization
                        if mark_queue:
                            mark_queue.pop(0)
                            
                    elif data['event'] == 'stop':
                        # Call ended - clean up connections
                        logger.info("Twilio call ended. Closing connections.")
                        if openai_ws.open:
                            logger.info("Closing OpenAI WebSocket.")
                            await openai_ws.close()
                            await log_websocket_status(openai_ws)
                        return
                        
            except WebSocketDisconnect:
                # Handle WebSocket disconnection
                print("Client disconnected.")
                if openai_ws.open:
                    await openai_ws.close()

        async def log_websocket_status(ws):
            """
            Debugging function to check if OpenAI WebSocket is closed properly.
            
            Args:
                ws (WebSocket): The WebSocket connection to check
            """
            if ws.open:
                logger.info("OpenAI WebSocket is still open.")
            else:
                logger.info("OpenAI WebSocket is now closed.")

        # === OPENAI TO TWILIO ===
        async def send_to_twilio():
            """
            Processes OpenAI responses and sends back audio to Twilio.
            
            This function continuously:
            1. Receives messages from OpenAI WebSocket
            2. Processes different event types (audio, content, speech detection)
            3. Forwards audio responses back to Twilio
            4. Handles interruptions when user starts speaking
            """
            nonlocal stream_sid, last_assistant_item, response_start_timestamp_twilio
            try:
                async for openai_message in openai_ws:
                    response = json.loads(openai_message)
                    
                    # Log selected event types for debugging
                    if response['type'] in LOG_EVENT_TYPES:
                        print(f"Received event: {response['type']}", response)

                    # Process audio responses from OpenAI
                    if response.get('type') == 'response.audio.delta' and 'delta' in response:
                        # Decode and re-encode audio for Twilio format
                        audio_payload = base64.b64encode(base64.b64decode(response['delta'])).decode('utf-8')
                        
                        # Create audio message for Twilio
                        audio_delta = {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {
                                "payload": audio_payload
                            }
                        }
                        await websocket.send_json(audio_delta)

                        # Track timing of AI response for interruption handling
                        if response_start_timestamp_twilio is None:
                            response_start_timestamp_twilio = latest_media_timestamp

                        # Save response ID for potential interruption handling
                        if response.get('item_id'):
                            last_assistant_item = response['item_id']

                        # Send a mark (synchronization point in the stream)
                        await send_mark(websocket, stream_sid)

                    # Detect when user starts speaking to handle interruptions
                    if response.get('type') == 'input_audio_buffer.speech_started':
                        print("Speech started detected.")
                        if last_assistant_item:
                            print(f"Interrupting response with id: {last_assistant_item}")
                            await handle_speech_started_event()
            except Exception as e:
                print(f"Error in send_to_twilio: {e}")

        async def handle_speech_started_event():
            """
            When user starts speaking again, truncate the current AI response.
            
            This provides a natural conversation flow by allowing the user to
            interrupt the AI's response, similar to human conversation.
            """
            nonlocal response_start_timestamp_twilio, last_assistant_item
            print("Handling speech started event.")
            
            # Only handle interruption if we have active marks and know when response started
            if mark_queue and response_start_timestamp_twilio is not None:
                # Calculate how long the response has been going
                elapsed_time = latest_media_timestamp - response_start_timestamp_twilio

                # If we have an active assistant message, truncate it at current point
                if last_assistant_item:
                    truncate_event = {
                        "type": "conversation.item.truncate",
                        "item_id": last_assistant_item,
                        "content_index": 0,
                        "audio_end_ms": elapsed_time  # Cut off at elapsed time
                    }
                    await openai_ws.send(json.dumps(truncate_event))

                # Clear any remaining audio queue in Twilio
                await websocket.send_json({
                    "event": "clear",
                    "streamSid": stream_sid
                })

                # Reset state variables
                mark_queue.clear()
                last_assistant_item = None
                response_start_timestamp_twilio = None

        async def send_mark(connection, stream_sid):
            """
            Send a 'mark' to Twilio for synchronization.
            
            These marks are used to track positions in the audio stream
            for features like interruption handling.
            
            Args:
                connection (WebSocket): The WebSocket connection to Twilio
                stream_sid (str): The unique stream identifier from Twilio
            """
            if stream_sid:
                mark_event = {
                    "event": "mark",
                    "streamSid": stream_sid,
                    "mark": {"name": "responsePart"}
                }
                await connection.send_json(mark_event)
                mark_queue.append('responsePart')

        # Run both coroutines (sending and receiving) concurrently
        # This allows bidirectional communication between Twilio and OpenAI
        await asyncio.gather(receive_from_twilio(), send_to_twilio())

# === Patient Data Retrieval ===
def get_patient_by_id(patient_id: str) -> dict:
    """
    Retrieve patient details from the database by patient ID.
    
    Args:
        patient_id (str): The ID of the patient to retrieve
        
    Returns:
        dict: Dictionary containing patient details with keys:
              'id', 'name', 'phone', 'action', 'medical_history', 'comments'
              
    Raises:
        Exception: If patient not found or database error occurs
    """
    try:
        # Establish database connection
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Execute SQL query to get patient details
        query = """
        SELECT 
            Id, 
            PatientName, 
            ContactNo, 
            Action, 
            MedicalHistory, 
            Comments
        FROM [Agentic AI Scheduling].[dbo].[patients]
        WHERE Id = ?
        """
        
        cursor.execute(query, patient_id)
        row = cursor.fetchone()
        
        if not row:
            # Log error if patient not found
            logger.error(f"Patient with ID {patient_id} not found")
            return None
            
        # Map the database columns to our expected dictionary keys
        patient_data = {
            "id": str(row.Id),
            "name": row.PatientName,
            "phone": row.ContactNo,
            "action": row.Action,
            "medical_history": row.MedicalHistory,
            "comments": row.Comments
        }
        
        logger.info(f"Retrieved patient data for ID {patient_id}: {patient_data}")
        return patient_data
        
    except pyodbc.Error as e:
        # Handle database-specific errors
        logger.error(f"Database error while fetching patient data: {str(e)}")
        raise Exception(f"Database error: {str(e)}")
    except Exception as e:
        # Handle any other unexpected errors
        logger.error(f"Unexpected error while fetching patient data: {str(e)}")
        raise Exception(f"Unexpected error: {str(e)}")
    finally:
        # Ensure connection is closed even if error occurs
        try:
            if 'conn' in locals() and conn:
                conn.close()
        except Exception as e:
            logger.error(f"Error closing database connection: {str(e)}")

# === Session Initialization Helpers ===
async def send_initial_conversation_item(openai_ws, patient_details: dict):
    """
    Send the system message and the first assistant message to OpenAI to begin the conversation,
    using patient details retrieved from the database.

    Parameters:
      openai_ws: The WebSocket connection to OpenAI.
      patient_details (dict): Contains 'name', 'action', and 'medical_history'.
    """
    # 1. Send a detailed system message to firmly set the assistant's persona.
    system_message_text = (
        "You are a helpful AI receptionist working at Allballa Dental Center. "
        "Please ignore any default greetings and use the following style when greeting the caller: "
        "'Hello there {name}! This is AI Dental Assistant OnasiHelper calling from Allballa Dental Center. "
        "{history_context}I'm reaching out regarding {action}. Your next follow-up appointment is due, "
        "and I'd like to schedule it for you. Do you have a preferred date and time?'. "
        "Always follow the center's protocols and provide accurate scheduling information."
    )
    # We'll later replace the placeholders using the actual patient details.
    
    # 2. Extract patient details for personalization
    name = patient_details.get("name", "there")  # Fallback to "there" if name not found
    action = patient_details.get("action", "your appointment")  # Fallback to generic appointment
    medical_history = patient_details.get("medical_history", "")  # Empty if no medical history
    
    # Include medical history context if available
    history_context = f"I see from your records that your medical history includes {medical_history}. " if medical_history else ""
    
    # Replace placeholders in the system message text with actual patient details
    system_message_text = system_message_text.format(name=name, history_context=history_context, action=action)
    
    # Create and send system message to OpenAI
    system_message_item = {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "system",
            "content": [
                {"type": "input_text", "text": system_message_text}
            ]
        }
    }
    await openai_ws.send(json.dumps(system_message_item))
    
    # 3. Build the assistant's initial greeting message with personalized details
    initial_text = (
        f"Hello there {name}! This is AI Dental Assistant OnasiHelper calling from Allballa Dental Center. "
        f"{history_context}"
        f"I'm reaching out regarding {action}. "
        f"Your next follow-up appointment is due, and I'd like to schedule it for you. "
        f"Do you have a preferred date and time?"
    )
    
    # Create and send the initial AI message to start the conversation
    initial_conversation_item = {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "input_text", "text": initial_text}
            ]
        }
    }
    await openai_ws.send(json.dumps(initial_conversation_item))
    
    # 4. Trigger the assistant to start speaking (generate audio response)
    await openai_ws.send(json.dumps({"type": "response.create"}))
    

async def send_session_update(openai_ws):
    """
    Send session settings like voice, audio format, and instructions to OpenAI.
    
    This configures the AI assistant's behavior and technical parameters
    for the current session.
    
    Args:
        openai_ws: The WebSocket connection to OpenAI
    """
    # Create session configuration with desired parameters
    session_update = {
    "type": "session.update",
    "session": {
        "turn_detection": {"type": "server_vad"},  # Voice activity detection
        "input_audio_format": "g711_ulaw",         # Twilio's audio format (µ-law)
        "output_audio_format": "g711_ulaw",        # Output in same format
        "voice": "alloy",                          # Voice model to use
        "instructions": "You are a helpful AI receptionist working at Allballa Dental Center. Ignore any default greetings. Use the provided custom greeting exactly.",
        "modalities": ["text", "audio"],           # Enable both text and audio
        "temperature": 0.8,                        # Creativity parameter (higher = more creative)
    }
    }
    print('Sending session update:', json.dumps(session_update))
    
    # Send session configuration to OpenAI
    await openai_ws.send(json.dumps(session_update))
    
    # Fetch patient details from database (hardcoded ID for demo)
    patient_details = get_patient_by_id("1")
    
    # Initialize conversation with patient-specific greeting
    await send_initial_conversation_item(openai_ws, patient_details)


@app.get("/make-call")
async def trigger_call():
    """Endpoint to initiate outbound call from Twilio to your phone"""
    # Load credentials from .env
    TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
    TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
    YOUR_NUMBER = os.getenv('YOUR_PHONE_NUMBER')  # Your real phone number (+1xxx...)
    TWILIO_NUMBER = os.getenv('TWILIO_PHONE_NUMBER')  # Your Twilio number
    
    # Initialize Twilio client
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    
    # Start call
    call = client.calls.create(
        url=f'https://{os.getenv("HOSTNAME")}/incoming-call',  # Uses existing endpoint
        to=YOUR_NUMBER,
        from_=TWILIO_NUMBER
    )
    
    return {"status": "Call initiated!", "call_sid": call.sid}

# === App Runner ===
if __name__ == "__main__":
  # Only run the server if script is executed directly
  import uvicorn  # Import here to avoid unnecessary dependency if imported as module
  
  # Start the FastAPI application with uvicorn
  uvicorn.run(app, host="0.0.0.0", port=PORT)  # Listen on all interfaces