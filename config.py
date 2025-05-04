"""
fixed_env_config.py
------------------
Module to properly load and parse environment variables for database connection.
"""

import os
from dotenv import load_dotenv
import logging

logger = logging.getLogger(__name__)

def load_clean_config():
    """
    Load environment variables from .env file and clean up any comments.
    Returns a clean configuration dictionary.
    """
    # Load environment variables
    load_dotenv()
    
    # Clean up environment variables that might have comments
    config = {}
    
    # Database connection parameters
    config["SQL_SERVER_DRIVER"] = os.getenv("SQL_SERVER_DRIVER", "").split('#')[0].strip()
    config["SQL_SERVER_SERVER"] = os.getenv("SQL_SERVER_SERVER", "").split('#')[0].strip()
    config["SQL_SERVER_DATABASE"] = os.getenv("SQL_SERVER_DATABASE", "").split('#')[0].strip()
    config["SQL_SERVER_TRUSTED_CONNECTION"] = os.getenv("SQL_SERVER_TRUSTED_CONNECTION", "").split('#')[0].strip()
    
    # API keys and other configuration
    config["OPENAI_API_KEY"] = os.getenv("OPENAI_API_KEY")
    config["TWILIO_ACCOUNT_SID"] = os.getenv("TWILIO_ACCOUNT_SID")
    config["TWILIO_AUTH_TOKEN"] = os.getenv("TWILIO_AUTH_TOKEN")
    config["TWILIO_PHONE_NUMBER"] = os.getenv("TWILIO_PHONE_NUMBER")
    config["YOUR_PHONE_NUMBER"] = os.getenv("YOUR_PHONE_NUMBER")
    config["NGROK_HOSTNAME"] = os.getenv("NGROK_HOSTNAME")
    config["PORT"] = int(os.getenv("PORT", 5050))
    
    # Log configuration (without sensitive values)
    logger.info("Loaded configuration:")
    logger.info(f"Database Driver: {config['SQL_SERVER_DRIVER']}")
    logger.info(f"Database Server: {config['SQL_SERVER_SERVER']}")
    logger.info(f"Database Name: {config['SQL_SERVER_DATABASE']}")
    logger.info(f"Trusted Connection: {config['SQL_SERVER_TRUSTED_CONNECTION']}")
    logger.info(f"Ngrok Hostname: {config.get('NGROK_HOSTNAME')}")
    logger.info(f"Port: {config['PORT']}")
    
    return config
