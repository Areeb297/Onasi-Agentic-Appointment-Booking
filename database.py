"""
robust_database.py
-----------------
Enhanced database module with robust transaction handling and connection management.
"""

import pyodbc
import logging
from datetime import datetime
import time
import os
from config import load_clean_config

# Configure logging
logger = logging.getLogger(__name__)

# Load clean configuration
config = load_clean_config()

def get_connection(max_retries=3, retry_delay=2):
    """
    Establishes and returns a connection to the SQL Server database with retry logic.
    
    Args:
        max_retries: Maximum number of connection attempts
        retry_delay: Delay between retries in seconds
    
    Returns:
        pyodbc.Connection: Database connection
    
    Raises:
        Exception: If connection fails after all retries
    """
    # Use the actual database name from config, not hardcoded
    actual_database = "Agentic AI Scheduling"
    
    conn_str = (
        f"DRIVER={config['SQL_SERVER_DRIVER']};"
        f"SERVER={config['SQL_SERVER_SERVER']};"
        f"DATABASE={actual_database};"  # Use the actual database name
        f"Trusted_Connection={config['SQL_SERVER_TRUSTED_CONNECTION']}"
    )
    
    logger.info(f"Connecting to database: {actual_database} on server {config['SQL_SERVER_SERVER']}")
    
    for attempt in range(max_retries):
        try:
            conn = pyodbc.connect(conn_str)
            logger.info(f"Connected to database: {actual_database} (Attempt {attempt+1}/{max_retries})")
            
            # Test the connection with a simple query
            cursor = conn.cursor()
            cursor.execute("SELECT @@VERSION")
            cursor.fetchone()
            
            # Set connection properties for better reliability
            conn.autocommit = False  # Ensure explicit transaction control
            
            return conn
        except pyodbc.Error as e:
            logger.error(f"Database connection error (Attempt {attempt+1}/{max_retries}): {str(e)}")
            if attempt < max_retries - 1:
                logger.info(f"Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
            else:
                logger.error("All connection attempts failed")
                raise

def execute_with_transaction(func, *args, **kwargs):
    """
    Execute a database function within a transaction with proper error handling.
    
    Args:
        func: The database function to execute
        *args: Arguments to pass to the function
        **kwargs: Keyword arguments to pass to the function
    
    Returns:
        The result of the function call
    """
    conn = None
    try:
        # Create a fresh connection for this transaction
        conn = get_connection()
        
        # Execute the function
        result = func(conn, *args, **kwargs)
        
        # Commit the transaction if successful
        conn.commit()
        logger.info("Transaction committed successfully")
        
        return result
    except Exception as e:
        logger.error(f"Transaction failed: {str(e)}")
        if conn:
            try:
                conn.rollback()
                logger.info("Transaction rolled back")
            except Exception as rollback_error:
                logger.error(f"Rollback failed: {str(rollback_error)}")
        raise
    finally:
        if conn and not conn.closed:
            conn.close()
            logger.info("Database connection closed")

def get_patient_by_id(conn, patient_id):
    """
    Retrieves patient details and available appointment slots by patient ID.
    
    Args:
        conn: Database connection
        patient_id: The ID of the patient to retrieve
        
    Returns:
        dict: Patient details including available slots
    """
    try:
        cursor = conn.cursor()
        patient_query = """
        SELECT Id, PatientName, ContactNo, Action, MedicalHistory, Comments
        FROM [Agentic AI Scheduling].[dbo].[patients]
        WHERE Id = ?
        """
        cursor.execute(patient_query, patient_id)
        patient_row = cursor.fetchone()
        
        if not patient_row:
            logger.info(f"No patient found with ID {patient_id}")
            return None

        # Get available slots with explicit schema reference
        slots_query = """
        SELECT 
            Id,
            CONVERT(varchar, Date, 23) AS Date,
            CONVERT(varchar, SlotStart, 108) AS StartTime,
            CONVERT(varchar, SlotEnd, 108) AS EndTime,
            Status
        FROM [Agentic AI Scheduling].[dbo].[DoctorSlots]
        WHERE Status = 'Available'
        """
        cursor.execute(slots_query)
        
        availability = []
        for row in cursor.fetchall():
            availability.append({
                "slot_id": row.Id,
                "date": row.Date,
                "start_time": row.StartTime.split('.')[0],
                "end_time": row.EndTime.split('.')[0],
                "status": row.Status,
                "display": f"{row.Date} {row.StartTime.split('.')[0]} to {row.EndTime.split('.')[0]}"
            })

        patient_details = {
            "id": str(patient_row.Id),
            "name": patient_row.PatientName,
            "phone": patient_row.ContactNo,
            "action": patient_row.Action,
            "medical_history": patient_row.MedicalHistory,
            "comments": patient_row.Comments,
            "availability": availability
        }
        logger.info(f"Retrieved data for patient ID {patient_id} with {len(availability)} available slots")
        return patient_details
    
    except pyodbc.Error as e:
        logger.error(f"Database error in get_patient_by_id: {str(e)}")
        raise

def _save_appointment_internal(conn, slot_id):
    """
    Internal function to save appointment by updating slot status and creating appointment record.
    
    Args:
        conn: Database connection
        slot_id: The ID of the slot to book
    
    Returns:
        bool: True if successful, False otherwise
    """
    cursor = conn.cursor()
    
    # First verify the slot exists and is available
    verify_query = """
    SELECT Id, Status 
    FROM [Agentic AI Scheduling].[dbo].[DoctorSlots]
    WHERE Id = ?
    """
    cursor.execute(verify_query, (slot_id,))
    slot = cursor.fetchone()
    
    if not slot:
        logger.error(f"Slot ID {slot_id} not found")
        return False
        
    if slot.Status != 'Available':
        logger.error(f"Slot ID {slot_id} is not available (status: {slot.Status})")
        return False
    
    # Update slot status to Booked with explicit schema reference
    update_query = """
    UPDATE [Agentic AI Scheduling].[dbo].[DoctorSlots]
    SET Status = 'Booked'
    WHERE Id = ?
    """
    cursor.execute(update_query, (slot_id,))
    rows_affected_update = cursor.rowcount
    logger.info(f"Updated slot status: {rows_affected_update} rows affected")
    
    if rows_affected_update == 0:
        logger.error(f"Failed to update slot status for ID {slot_id}")
        return False
    
    # Create appointment record with explicit schema reference
    insert_query = """
    INSERT INTO [Agentic AI Scheduling].[dbo].[DoctorAppointments]
    (DocId, SlotId, PatientId, Status, CreatedAt)
    VALUES (1, ?, 1, 'Confirmed', GETDATE())
    """
    cursor.execute(insert_query, (slot_id,))
    rows_affected_insert = cursor.rowcount
    logger.info(f"Inserted appointment record: {rows_affected_insert} rows affected")
    
    if rows_affected_insert == 0:
        logger.error(f"Failed to insert appointment record for slot ID {slot_id}")
        return False
    
    # Verify the changes before committing
    verify_slot_query = """
    SELECT Status 
    FROM [Agentic AI Scheduling].[dbo].[DoctorSlots]
    WHERE Id = ?
    """
    cursor.execute(verify_slot_query, (slot_id,))
    updated_slot = cursor.fetchone()
    
    if not updated_slot or updated_slot.Status != 'Booked':
        logger.error(f"Verification failed: Slot {slot_id} status is not 'Booked'")
        return False
    
    verify_appointment_query = """
    SELECT COUNT(*) as count
    FROM [Agentic AI Scheduling].[dbo].[DoctorAppointments]
    WHERE SlotId = ?
    """
    cursor.execute(verify_appointment_query, (slot_id,))
    appointment_count = cursor.fetchone().count
    
    if appointment_count == 0:
        logger.error(f"Verification failed: No appointment record found for slot ID {slot_id}")
        return False
    
    # Log appointment details for verification
    details_query = """
    SELECT 
        a.Id AS AppointmentId,
        p.PatientName,
        CONVERT(varchar, s.Date, 23) AS AppointmentDate,
        CONVERT(varchar, s.SlotStart, 108) AS StartTime,
        CONVERT(varchar, s.SlotEnd, 108) AS EndTime,
        a.Status
    FROM [Agentic AI Scheduling].[dbo].[DoctorAppointments] a
    JOIN [Agentic AI Scheduling].[dbo].[DoctorSlots] s ON a.SlotId = s.Id
    JOIN [Agentic AI Scheduling].[dbo].[patients] p ON a.PatientId = p.Id
    WHERE a.SlotId = ?
    """
    cursor.execute(details_query, (slot_id,))
    appointment = cursor.fetchone()
    
    if appointment:
        logger.info(f"Appointment details: ID={appointment.AppointmentId}, "
                   f"Patient={appointment.PatientName}, "
                   f"Date={appointment.AppointmentDate}, "
                   f"Time={appointment.StartTime}-{appointment.EndTime}, "
                   f"Status={appointment.Status}")
    
    return True

def save_appointment(slot_id):
    """
    Public function to save appointment with transaction handling.
    
    Args:
        slot_id: The ID of the slot to book
    
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        return execute_with_transaction(_save_appointment_internal, slot_id)
    except Exception as e:
        logger.error(f"Failed to save appointment: {str(e)}")
        return False

def get_appointment_by_slot(slot_id):
    """
    Retrieve appointment details by slot ID.
    
    Args:
        slot_id: The ID of the slot
    
    Returns:
        dict: Appointment details or None if not found
    """
    try:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            query = """
            SELECT 
                a.Id AS AppointmentId,
                p.PatientName,
                p.ContactNo,
                CONVERT(varchar, s.Date, 23) AS AppointmentDate,
                CONVERT(varchar, s.SlotStart, 108) AS StartTime,
                CONVERT(varchar, s.SlotEnd, 108) AS EndTime,
                a.Status,
                a.CreatedAt
            FROM [Agentic AI Scheduling].[dbo].[DoctorAppointments] a
            JOIN [Agentic AI Scheduling].[dbo].[DoctorSlots] s ON a.SlotId = s.Id
            JOIN [Agentic AI Scheduling].[dbo].[patients] p ON a.PatientId = p.Id
            WHERE a.SlotId = ?
            """
            cursor.execute(query, (slot_id,))
            row = cursor.fetchone()
            
            if not row:
                logger.info(f"No appointment found for slot ID {slot_id}")
                return None
                
            appointment = {
                "id": row.AppointmentId,
                "patient_name": row.PatientName,
                "contact": row.ContactNo,
                "date": row.AppointmentDate,
                "start_time": row.StartTime,
                "end_time": row.EndTime,
                "status": row.Status,
                "created_at": row.CreatedAt.isoformat() if row.CreatedAt else None
            }
            
            logger.info(f"Retrieved appointment for slot ID {slot_id}")
            return appointment
        finally:
            if conn and not conn.closed:
                conn.close()
                logger.info("Database connection closed")
    except pyodbc.Error as e:
        logger.error(f"Database error in get_appointment_by_slot: {str(e)}")
        return None

def get_patient_appointments(patient_id):
    """
    Retrieve all appointments for a patient.
    
    Args:
        patient_id: The ID of the patient
    
    Returns:
        list: List of appointment details
    """
    try:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            query = """
            SELECT 
                a.Id AS AppointmentId,
                a.SlotId,
                CONVERT(varchar, s.Date, 23) AS AppointmentDate,
                CONVERT(varchar, s.SlotStart, 108) AS StartTime,
                CONVERT(varchar, s.SlotEnd, 108) AS EndTime,
                a.Status,
                a.CreatedAt
            FROM [Agentic AI Scheduling].[dbo].[DoctorAppointments] a
            JOIN [Agentic AI Scheduling].[dbo].[DoctorSlots] s ON a.SlotId = s.Id
            WHERE a.PatientId = ?
            ORDER BY s.Date, s.SlotStart
            """
            cursor.execute(query, (patient_id,))
            
            appointments = []
            for row in cursor.fetchall():
                appointments.append({
                    "id": row.AppointmentId,
                    "slot_id": row.SlotId,
                    "date": row.AppointmentDate,
                    "start_time": row.StartTime,
                    "end_time": row.EndTime,
                    "status": row.Status,
                    "created_at": row.CreatedAt.isoformat() if row.CreatedAt else None
                })
                
            logger.info(f"Retrieved {len(appointments)} appointments for patient ID {patient_id}")
            return appointments
        finally:
            if conn and not conn.closed:
                conn.close()
                logger.info("Database connection closed")
    except pyodbc.Error as e:
        logger.error(f"Database error in get_patient_appointments: {str(e)}")
        return []

def verify_database_access():
    """
    Verify database access and connection parameters.
    
    Returns:
        dict: Verification results
    """
    results = {
        "connection_success": False,
        "database_exists": False,
        "tables_exist": False,
        "test_transaction": False,
        "errors": []
    }
    
    try:
        # Test connection
        conn = get_connection()
        results["connection_success"] = True
        
        try:
            cursor = conn.cursor()
            
            # Check if database exists
            cursor.execute("SELECT DB_ID('Agentic AI Scheduling')")
            db_id = cursor.fetchone()[0]
            results["database_exists"] = db_id is not None
            
            if not results["database_exists"]:
                results["errors"].append("Database 'Agentic AI Scheduling' does not exist")
                return results
            
            # Check if tables exist
            tables_exist = True
            tables_to_check = ['DoctorSlots', 'DoctorAppointments', 'patients']
            for table in tables_to_check:
                cursor.execute(f"SELECT OBJECT_ID('[Agentic AI Scheduling].[dbo].[{table}]')")
                table_id = cursor.fetchone()[0]
                if not table_id:
                    tables_exist = False
                    results["errors"].append(f"Table '{table}' does not exist")
            
            results["tables_exist"] = tables_exist
            
            if not tables_exist:
                return results
            
            # Test transaction
            try:
                # Get an available slot for testing
                cursor.execute("""
                SELECT TOP 1 Id
                FROM [Agentic AI Scheduling].[dbo].[DoctorSlots]
                WHERE Status = 'Available'
                """)
                row = cursor.fetchone()
                
                if not row:
                    results["errors"].append("No available slots found for testing")
                    return results
                
                test_slot_id = row.Id
                
                # Update slot status temporarily
                cursor.execute("""
                UPDATE [Agentic AI Scheduling].[dbo].[DoctorSlots]
                SET Status = 'Testing'
                WHERE Id = ?
                """, test_slot_id)
                
                # Verify the update
                cursor.execute("""
                SELECT Status
                FROM [Agentic AI Scheduling].[dbo].[DoctorSlots]
                WHERE Id = ?
                """, test_slot_id)
                updated_status = cursor.fetchone().Status
                
                # Reset the status
                cursor.execute("""
                UPDATE [Agentic AI Scheduling].[dbo].[DoctorSlots]
                SET Status = 'Available'
                WHERE Id = ?
                """, test_slot_id)
                
                # Commit the transaction
                conn.commit()
                
                results["test_transaction"] = updated_status == 'Testing'
                
                if not results["test_transaction"]:
                    results["errors"].append("Test transaction failed: status not updated")
            
            except Exception as e:
                results["errors"].append(f"Transaction test error: {str(e)}")
                conn.rollback()
        
        finally:
            if conn and not conn.closed:
                conn.close()
    
    except Exception as e:
        results["errors"].append(f"Connection error: {str(e)}")
    
    return results

# For testing
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    
    # Verify database access
    verification = verify_database_access()
    print("Database Verification Results:")
    print(f"Connection Success: {verification['connection_success']}")
    print(f"Database Exists: {verification['database_exists']}")
    print(f"Tables Exist: {verification['tables_exist']}")
    print(f"Test Transaction: {verification['test_transaction']}")
    
    if verification["errors"]:
        print("Errors:")
        for error in verification["errors"]:
            print(f"- {error}")
    
    if all([verification['connection_success'], verification['database_exists'], 
            verification['tables_exist'], verification['test_transaction']]):
        print("All database verification tests passed!")
    else:
        print("Some database verification tests failed!")
