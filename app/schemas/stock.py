from pydantic import BaseModel
from typing import Optional, List, Dict

class StockPrediction(BaseModel):
    stock: str
    last_price: float
    predicted_price: float
    rise_probability: float
    recommendation: str
    analysis: str

class UpdateResponse(BaseModel):
    success: bool
    message: str
    total_records: int = 0
    updated_records: int = 0