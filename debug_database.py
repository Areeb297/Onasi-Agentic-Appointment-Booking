"""
debug_database.py
----------------
Script to test database connection with different connection string formats.
"""

import pyodbc
import logging
from datetime import datetime
import time
import os
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def load_config():
    """Load configuration from .env file."""
    load_dotenv()
    
    config = {
        "SQL_SERVER_DRIVER": os.getenv("SQL_SERVER_DRIVER", ""),
        "SQL_SERVER_SERVER": os.getenv("SQL_SERVER_SERVER", ""),
        "SQL_SERVER_DATABASE": os.getenv("SQL_SERVER_DATABASE", ""),
        "SQL_SERVER_TRUSTED_CONNECTION": os.getenv("SQL_SERVER_TRUSTED_CONNECTION", "")
    }
    
    return config

def test_connection_string(server_format, database_name="Agentic AI Scheduling"):
    """Test a specific connection string format."""
    config = load_config()
    
    # Override the server format
    server = server_format
    
    conn_str = (
        f"DRIVER={config['SQL_SERVER_DRIVER']};"
        f"SERVER={server};"
        f"DATABASE={database_name};"
        f"Trusted_Connection={config['SQL_SERVER_TRUSTED_CONNECTION']}"
    )
    
    logger.info(f"Testing connection string: {conn_str}")
    
    try:
        conn = pyodbc.connect(conn_str, timeout=5)
        logger.info("✅ Connection successful!")
        
        # Test a simple query
        cursor = conn.cursor()
        cursor.execute("SELECT @@VERSION")
        version = cursor.fetchone()[0]
        logger.info(f"SQL Server version: {version[:50]}...")
        
        # Test access to the DoctorSlots table
        try:
            cursor.execute("SELECT TOP 1 * FROM [Agentic AI Scheduling].[dbo].[DoctorSlots]")
            row = cursor.fetchone()
            if row:
                logger.info(f"✅ Successfully queried DoctorSlots table. First row ID: {row.Id}")
            else:
                logger.info("✅ Successfully queried DoctorSlots table but it's empty.")
        except Exception as e:
            logger.error(f"❌ Error querying DoctorSlots table: {str(e)}")
        
        conn.close()
        return True
    except Exception as e:
        logger.error(f"❌ Connection failed: {str(e)}")
        return False

def test_save_appointment(server_format, slot_id=5):
    """Test saving an appointment with a specific connection string format."""
    config = load_config()
    
    # Override the server format
    server = server_format
    
    conn_str = (
        f"DRIVER={config['SQL_SERVER_DRIVER']};"
        f"SERVER={server};"
        f"DATABASE=Agentic AI Scheduling;"
        f"Trusted_Connection={config['SQL_SERVER_TRUSTED_CONNECTION']}"
    )
    
    logger.info(f"Testing save_appointment with connection string: {conn_str}")
    
    conn = None
    try:
        # Create a fresh connection for this transaction
        conn = pyodbc.connect(conn_str, timeout=5)
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
            
        logger.info(f"Found slot ID {slot_id} with status: {slot.Status}")
        
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
            conn.rollback()
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
            conn.rollback()
            return False
        
        # Commit the transaction
        conn.commit()
        logger.info(f"✅ Transaction committed successfully for slot ID {slot_id}")
        return True
    except Exception as e:
        logger.error(f"❌ Error saving appointment: {str(e)}")
        if conn:
            try:
                conn.rollback()
                logger.info("Transaction rolled back due to error")
            except Exception as rollback_error:
                logger.error(f"Rollback failed: {str(rollback_error)}")
        return False
    finally:
        if conn and not conn.closed:
            conn.close()
            logger.info("Database connection closed")

def main():
    """Test different connection string formats."""
    config = load_config()
    original_server = config["SQL_SERVER_SERVER"]
    
    logger.info(f"Original server string from .env: {original_server}")
    
    # Test different server formats
    server_formats = [
        original_server,                      # Original format
        original_server.replace("\\", "\\\\"),  # Double backslash
        original_server.replace("\\", "/"),     # Forward slash
        "localhost"                           # No instance name
    ]
    
    results = {}
    
    for i, server_format in enumerate(server_formats):
        logger.info(f"\nTest {i+1}: Testing server format: {server_format}")
        connection_success = test_connection_string(server_format)
        results[server_format] = connection_success
    
    # Summary of connection tests
    logger.info("\n=== Connection Test Results ===")
    for server_format, success in results.items():
        logger.info(f"Server format '{server_format}': {'✅ Success' if success else '❌ Failed'}")
    
    # Find the first successful format
    successful_format = next((fmt for fmt, success in results.items() if success), None)
    
    # If we found a working format, test saving an appointment
    if successful_format:
        logger.info(f"\n=== Testing Appointment Saving with Working Format ===")
        logger.info(f"Using server format: {successful_format}")
        save_success = test_save_appointment(successful_format)
        
        if save_success:
            logger.info("\n✅ APPOINTMENT SAVED SUCCESSFULLY!")
            logger.info(f"Use this connection string format in your .env file:")
            logger.info(f"SQL_SERVER_SERVER={successful_format}")
        else:
            logger.info("\n❌ APPOINTMENT SAVING FAILED")
            logger.info("Check the logs for detailed error messages")
    else:
        logger.error("\nNo working connection string format found!")
        logger.error("Please check your SQL Server configuration and credentials")

if __name__ == "__main__":
    main()
