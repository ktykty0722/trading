from fastapi import APIRouter, HTTPException
from typing import List
import pandas as pd
from app.db.supabase import supabase
from app.schemas.stock import StockPrediction

router = APIRouter()

@router.get("/predictions", summary="주식 예측 결과 조회", response_model=List[StockPrediction])
def read_predictions():
    try:
        # CSV 파일에서 예측 결과를 읽어오는 예시
        df = pd.read_csv("final_stock_analysis.csv")
        
        predictions = []
        for _, row in df.iterrows():
            predictions.append(
                StockPrediction(
                    stock=row["Stock"],
                    last_price=row["Last Actual Price"],
                    predicted_price=row["Predicted Future Price"],
                    rise_probability=row["Rise Probability (%)"],
                    recommendation=row["Recommendation"],
                    analysis=row["Analysis"]
                )
            )
        return predictions
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"예측 결과 조회 중 오류 발생: {str(e)}")

@router.get("/{ticker}", summary="특정 주식 정보 조회")
def read_stock_info(ticker: str):
    try:
        # Supabase에서 특정 주식 정보를 조회
        response = supabase.table("stocks").select("*").eq("symbol", ticker).execute()
        if not response.data:
            raise HTTPException(status_code=404, detail=f"{ticker} 주식 정보를 찾을 수 없습니다.")
        return response.data[0]
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"주식 정보 조회 중 오류 발생: {str(e)}")