# Dental Scheduler Voice Agent

## Overview

The **Dental Scheduler Voice Agent** is an AI-powered application designed to automate appointment scheduling for Allballa Dental Center. Built with **FastAPI**, **Twilio**, **OpenAI Realtime API**, and **SQL Server**, it handles incoming and outgoing calls, processes natural language to schedule appointments, and integrates with a robust database for reliable transaction management. The agent uses voice interactions to greet patients, retrieve available slots, confirm appointments, and save bookings to the database.

Key features:
- **Voice Interaction**: Handles real-time voice conversations using Twilio's media streaming and OpenAI's Realtime API.
- **Appointment Scheduling**: Parses user speech to identify preferred dates/times and books appointments based on availability.
- **Database Integration**: Uses a SQL Server database with robust transaction handling to store and retrieve patient and appointment data.
- **Error Handling**: Includes retry logic for database connections and detailed logging for debugging.
- **Customizable Configuration**: Loads environment variables for easy setup and configuration.

## Prerequisites

Before running the application, ensure you have the following installed:
- Python 3.8+
- SQL Server (e.g., SQL Server Express)
- Twilio account with a phone number
- OpenAI API key
- Git (optional, for cloning the repository)

## Installation

1. **Clone the Repository** (if applicable):
   ```bash
   git clone <repository-url>
   cd dental-scheduler-voice-agent
   ```

2. **Create a Virtual Environment**:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install Dependencies**:
   Install the required Python packages using the provided `requirements.txt` (create one if not present, see below).
   ```bash
   pip install -r requirements.txt
   ```

   Example `requirements.txt`:
   ```
   fastapi==0.115.0
   uvicorn==0.30.6
   pyodbc==5.1.0
   python-dotenv==1.0.1
   twilio==9.2.3
   websockets==12.0
   python-dateutil==2.9.0
   ```

4. **Set Up SQL Server**:
   - Ensure SQL Server is running (e.g., `localhost\SQLEXPRESS`).
   - Create a database named `Agentic AI Scheduling`.
   - Create the required tables (`patients`, `DoctorSlots`, `DoctorAppointments`). Example schema:
     ```sql
     CREATE TABLE patients (
         Id INT PRIMARY KEY,
         PatientName NVARCHAR(100),
         ContactNo NVARCHAR(20),
         Action NVARCHAR(50),
         MedicalHistory NVARCHAR(MAX),
         Comments NVARCHAR(MAX)
     );

     CREATE TABLE DoctorSlots (
         Id INT PRIMARY KEY,
         Date DATE,
         SlotStart TIME,
         SlotEnd TIME,
         Status NVARCHAR(20)
     );

     CREATE TABLE DoctorAppointments (
         Id INT PRIMARY KEY IDENTITY,
         DocId INT,
         SlotId INT,
         PatientId INT,
         Status NVARCHAR(20),
         CreatedAt DATETIME
     );
     ```
   - Populate `DoctorSlots` with available slots for testing.

5. **Configure Environment Variables**:
   - Copy the `example.env` file to `.env`:
     ```bash
     cp example.env .env
     ```
   - Update `.env` with your credentials and settings:
     ```
     OPENAI_API_KEY=your_openai_api_key
     TWILIO_ACCOUNT_SID=your_twilio_account_sid
     TWILIO_AUTH_TOKEN=your_twilio_auth_token
     TWILIO_PHONE_NUMBER=+1234567890
     YOUR_PHONE_NUMBER=+0987654321
     SQL_SERVER_DRIVER={ODBC Driver 17 for SQL Server}
     SQL_SERVER_SERVER=localhost\SQLEXPRESS
     SQL_SERVER_DATABASE=Agentic AI Scheduling
     SQL_SERVER_TRUSTED_CONNECTION=yes
     HOSTNAME=your-ngrok-hostname
     PORT=5050
     ```

6. **Set Up Ngrok (for Twilio WebSocket)**:
   - Install [ngrok](https://ngrok.com/) to expose your local server.
   - Run ngrok to forward port 5050:
     ```bash
     ngrok http 5050
     ```
   - Update the `HOSTNAME` in `.env` with the ngrok URL (e.g., `abc123.ngrok.io`).

## Running the Application

1. **Start the FastAPI Server**:
   ```bash
   python main.py
   ```
   The server will run on `http://0.0.0.0:5050` (or the port specified in `.env`).

2. **Test the Application**:
   - **Verify Database Connection**:
     Access the `/verify-database` endpoint:
     ```bash
     curl http://localhost:5050/verify-database
     ```
     This checks the database connection, table existence, and transaction handling.
   - **Trigger an Outbound Call**:
     Use the `/make-call` endpoint to initiate a call:
     ```bash
     curl http://localhost:5050/make-call
     ```
     The agent will call the `YOUR_PHONE_NUMBER` and start a conversation.
   - **Handle Incoming Calls**:
     Configure your Twilio phone number to point to `https://<your-ngrok-hostname>/incoming-call` for incoming calls.

## Usage

- **Greeting**: The agent greets the patient using their name and medical history (if available) and offers to schedule an appointment.
- **Scheduling**: The patient can mention a preferred date/time. The agent checks availability, confirms the slot, and saves the appointment to the database.
- **Confirmation**: Upon successful booking, the agent uses one of the exact phrases (e.g., "I have scheduled your appointment for [DATE/TIME]") and saves the appointment.
- **Error Handling**: If a slot is unavailable or a database error occurs, the agent suggests alternative slots or asks for clarification.

Example interaction:
```
Agent: Hello there John! This is AI Dental Assistant OnasiHelper calling from Allballa Dental Center. Your next follow-up appointment is due, and I'd like to schedule it for you. Do you have a preferred date and time? Our available slots are: 2025-04-15 09:00 to 10:00, 2025-04-15 10:00 to 11:00...
Patient: Can I have April 15th at 9 AM?
Agent: I found an available slot on April 15th, 2025 from 9:00 AM to 10:00 AM. Would you like me to book this slot for you?
Patient: Yes, please.
Agent: I have scheduled your appointment for April 15th, 2025 from 9:00 AM to 10:00 AM. Your appointment has been confirmed and saved to our system. Thank you!
```

## Project Structure

```
dental-scheduler-voice-agent/
├── config.py               # Loads and cleans environment variables
├── database.py             # Manages SQL Server connections and transactions
├── debug_database.py       # Tests database connection formats
├── main.py                 # FastAPI application entry point
├── models.py               # Defines patient data model
├── openai_handler.py       # Handles OpenAI Realtime API interactions
├── twilio_handler.py       # Manages Twilio call handling and WebSocket
├── example.env             # Example environment configuration
├── confirmation_debug.log  # Debug log for appointment confirmations
├── requirements.txt        # Python dependencies (create this file)
```

## Debugging

- **Logs**: Check logs in the console or `confirmation_debug.log` for appointment confirmation issues.
- **Database Issues**: Run `debug_database.py` to test connection strings:
  ```bash
  python debug_database.py
  ```
- **WebSocket Errors**: Ensure ngrok is running and the `HOSTNAME` matches the ngrok URL.
- **API Keys**: Verify that `OPENAI_API_KEY`, `TWILIO_ACCOUNT_SID`, and `TWILIO_AUTH_TOKEN` are correct.

## Contributing

Contributions are welcome! Please:
1. Fork the repository.
2. Create a feature branch (`git checkout -b feature/YourFeature`).
3. Commit changes (`git commit -m 'Add YourFeature'`).
4. Push to the branch (`git push origin feature/YourFeature`).
5. Open a pull request.

## License

This project is licensed under the MIT License. See the `LICENSE` file for details.

## Contact

For questions or support, contact the project maintainer at [areeb.shafqat@gmail.com].

---

This README assumes some standard practices (e.g., using a `requirements.txt` file) and includes placeholders (e.g., repository URL, email) that you should replace with actual values. Let me know if you need help refining any section or adding specific details!