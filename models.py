"""
models.py
---------
Defines data models for patient and appointment data to ensure type safety and clarity.
Uses Python dictionaries for simplicity, matching the original code's structure.
"""

from typing import List, Dict, Optional

def create_patient_model(
    id: str,
    name: str,
    phone: str,
    action: str,
    medical_history: str,
    comments: str,
    availability: List[Dict]
) -> Dict:
    """
    Creates a patient data model with the specified attributes.
    
    Args:
        id (str): Patient's unique identifier.
        name (str): Patient's name.
        phone (str): Patient's contact number.
        action (str): Action to take (e.g., scheduling).
        medical_history (str): Patient's medical history.
        comments (str): Additional comments.
        availability (List[Dict]): List of available appointment slots.
    
    Returns:
        dict: Structured patient data.
    """
    return {
        "id": id,
        "name": name,
        "phone": phone,
        "action": action,
        "medical_history": medical_history,
        "comments": comments,
        "availability": availability
    }