import pandas as pd
from app.db.supabase import supabase
# stock.py는 아직 모듈로 옮기지 않았으므로 기존 임포트 유지
from stock import collect_economic_data
import stock
import numpy as np
from datetime import datetime, timedelta
import pytz
from app.core.config import settings
from fastapi import APIRouter, BackgroundTasks, HTTPException, Depends
from app.services.stock_recommendation_service import StockRecommendationService

def get_last_updated_date():
    """
    데이터베이스에서 마지막으로 수집된 날짜를 조회합니다.
    """
    try:
        # 날짜 컬럼명을 올바르게 수정
        response = supabase.table("economic_and_stock_data").select("날짜").order("날짜", desc=True).limit(1).execute()
        
        if response.data and len(response.data) > 0:
            last_date = datetime.fromisoformat(response.data[0]["날짜"].replace('Z', '+00:00'))
            # 다음 날짜 반환
            next_date = (last_date + timedelta(days=1)).strftime('%Y-%m-%d')
            print(f"마지막 수집 날짜: {last_date.strftime('%Y-%m-%d')}, 다음 수집 시작일: {next_date}")
            return next_date
        else:
            # 데이터가 없으면 기본 시작 날짜 반환 (2006-01-01)
            print("기존 데이터가 없습니다. 기본 시작 날짜(2006-01-01)로 설정합니다.")
            return "2006-01-01"
    except Exception as e:
        print(f"마지막 수집 날짜 조회 중 오류 발생: {str(e)}")
        # 오류 발생 시 기본 시작 날짜 반환
        return "2006-01-01"

def get_existing_data_with_nulls():
    """
    NULL 값이 있는 기존 데이터를 조회합니다.
    """
    try:
        # NULL 값이 있는 레코드만 조회 (PostgreSQL의 JSON 연산자 사용)
        query = "SELECT * FROM economic_and_stock_data WHERE jsonb_object_keys(data::jsonb) @> '{null}'::jsonb"
        response = supabase.table("economic_and_stock_data").select("*").execute(query)
        
        if response.data and len(response.data) > 0:
            # Pandas DataFrame으로 변환
            df = pd.DataFrame(response.data)
            print(f"NULL 값이 포함된 레코드 {len(df)}개를 찾았습니다.")
            return df
        else:
            print("NULL 값이 포함된 레코드가 없습니다.")
            return pd.DataFrame()
    except Exception as e:
        print(f"NULL 값 데이터 조회 중 오류 발생: {str(e)}")
        return pd.DataFrame()

# 주가 관련 컬럼 목록 정의
stock_columns = [
    "나스닥 종합지수", "S&P 500 지수", "금 가격", "달러 인덱스", "나스닥 100", 
    "S&P 500 ETF", "QQQ ETF", "러셀 2000 ETF", "다우 존스 ETF", "VIX 지수", 
    "닛케이 225", "상해종합", "항셍", "영국 FTSE", "독일 DAX", "프랑스 CAC 40", 
    "미국 전체 채권시장 ETF", "TIPS ETF", "투자등급 회사채 ETF", "달러/엔", "달러/위안",
    "미국 리츠 ETF", "애플", "마이크로소프트", "아마존", "구글 A", "구글 C", "메타", 
    "테슬라", "엔비디아", "코스트코", "넷플릭스", "페이팔", "인텔", "시스코", "컴캐스트", 
    "펩시코", "암젠", "허니웰 인터내셔널", "스타벅스", "몬델리즈", "마이크론", "브로드컴", 
    "어도비", "텍사스 인스트루먼트", "AMD", "어플라이드 머티리얼즈"
]

# 경제 지표 컬럼 목록 정의
economic_columns = [
    "10년 기대 인플레이션율", "장단기 금리차", "기준금리", "미시간대 소비자 심리지수", 
    "실업률", "2년 만기 미국 국채 수익률", "10년 만기 미국 국채 수익률", "금융스트레스지수", 
    "개인 소비 지출", "소비자 물가지수", "5년 변동금리 모기지", "미국 달러 환율", 
    "통화 공급량 M2", "가계 부채 비율", "GDP 성장률"
]

async def update_economic_data_in_background(force: bool = False):
    """
    백그라운드에서 경제 지표 데이터를 업데이트
    force=True이면 장 중 체크를 무시하고 강제 수집
    """
    try:
        print("경제 지표 및 주가 데이터 업데이트 작업 시작...")

        # 미국 장 마감 여부 확인 (서머타임 여부와 관계없이 22:30~06:00는 미국 장 시간으로 처리)
        now = datetime.now()
        korea_time = now.strftime('%H:%M')
        current_hour = int(korea_time.split(':')[0])
        current_min = int(korea_time.split(':')[1])

        # 미국 장 시간인지 확인 (22:30~06:00)
        is_market_hours = False

        # 22:30 이후
        if current_hour >= 22 and (current_hour > 22 or current_min >= 30):
            is_market_hours = True
        # 다음 날 06:00 이전
        elif current_hour < 6:
            is_market_hours = True

        # 미국 주식 시장이 열려 있는 경우, 데이터 수집 연기 (force=True이면 무시)
        if is_market_hours and not force:
            print(f"현재 시간 {korea_time}은 미국 주식 시장 운영 시간입니다. 장 마감 후에 데이터를 수집합니다.")
            return

        if force and is_market_hours:
            print(f"현재 시간 {korea_time}은 장 중이지만, 강제 수집 모드로 실행합니다.")

        # 마지막 수집 날짜 조회
        start_date = get_last_updated_date()
        
        # 현재 날짜 계산
        today = datetime.now().strftime('%Y-%m-%d')
        yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        
        # 데이터 수집은 오늘까지 하되, 저장은 어제까지만
        collection_end_date = today
        storage_end_date = yesterday
        
        # 수집 시작일이 종료일보다 크면 종료
        if start_date > storage_end_date:
            print(f"수집 시작일({start_date})이 저장 종료일({storage_end_date})보다 큽니다. 수집할 데이터가 없습니다.")
            return {"success": True, "total_records": 0, "updated_records": 0}
        
        # 이전 데이터 가져오기 (마지막 수집 날짜의 데이터)
        previous_date = (datetime.strptime(start_date, '%Y-%m-%d') - timedelta(days=1)).strftime('%Y-%m-%d')
        prev_data_response = supabase.table("economic_and_stock_data").select("*").eq("날짜", previous_date).execute()
        previous_data = prev_data_response.data[0] if prev_data_response.data else {}
        
        # 데이터 수집 (오늘까지 수집)
        new_data = collect_economic_data(start_date=start_date, end_date=collection_end_date)
        
        # 디버깅: 수집된 데이터 확인
        print("\n=== 수집된 데이터 확인 ===")
        for date_idx in new_data.index:
            date_str = date_idx.strftime('%Y-%m-%d') if isinstance(date_idx, pd.Timestamp) else date_idx
            print(f"날짜: {date_str}")
            for stock in stock_columns[:5]:  # 몇 개의 주가만 출력
                if stock in new_data.columns:
                    print(f"  {stock}: {new_data.loc[date_idx, stock]}")
        
        if new_data is None or new_data.empty:
            print("수집할 새 데이터가 없습니다.")
            return {"success": True, "total_records": 0, "updated_records": 0}
        
        # 날짜 범위 생성 (시작일부터 어제까지만)
        all_dates = pd.date_range(start=start_date, end=storage_end_date)
        saved_count = 0
        
        # 어제까지 날짜에 대해서만 처리
        for date in all_dates:
            date_str = date.strftime('%Y-%m-%d')
            
            # 해당 날짜의 데이터가 수집되었는지 확인
            if date in new_data.index:
                row = new_data.loc[date]
                print(f"\n== {date_str} 데이터가 있음 (저장 대상) ==")
                # 주요 주가 데이터 몇 개 출력
                for stock in stock_columns[:5]:
                    if stock in row.index:
                        print(f"  원본 {stock}: {row[stock]}")
            else:
                print(f"\n== {date_str} 데이터가 없음, 이전 데이터 사용 (저장 대상) ==")
                row = pd.Series(dtype='object')
            
            # 기존 데이터 확인
            check = supabase.table("economic_and_stock_data").select("*").eq("날짜", date_str).execute()
            
            # 데이터 딕셔너리 생성
            data_dict = {}
            for col_name, value in row.items():
                if not pd.isna(value):  # null이 아닌 값만 포함
                    data_dict[col_name] = value
            
            # 이전 데이터로 null 값 채우기 (모든 컬럼 대상)
            for col_name, value in previous_data.items():
                if col_name not in ("날짜", "id") and col_name not in data_dict and value is not None:
                    data_dict[col_name] = value
            
            # 중복 방지를 위해 기존 데이터가 있으면 업데이트, 없으면 삽입
            if check.data and len(check.data) > 0:
                # 기존 레코드가 있는 경우, null 값만 업데이트
                existing_data = check.data[0]
                update_dict = {}
                
                for col_name, value in data_dict.items():
                    # 기존 값이 null이거나 누락된 경우에만 업데이트
                    if col_name not in existing_data or existing_data[col_name] is None:
                        update_dict[col_name] = value
                
                if update_dict:  # 업데이트할 값이 있는 경우에만
                    supabase.table("economic_and_stock_data").update(update_dict).eq("날짜", date_str).execute()
            else:
                # 새 레코드 추가
                insert_dict = {"날짜": date_str}
                insert_dict.update(data_dict)
                supabase.table("economic_and_stock_data").insert(insert_dict).execute()
            
            # 현재 데이터를 다음 날짜 처리를 위한 이전 데이터로 설정
            if data_dict:  # 데이터가 있는 경우에만
                previous_data = {"날짜": date_str}
                previous_data.update(data_dict)
            
            # 주요 주가 데이터 출력
            for stock in stock_columns[:5]:
                if stock in data_dict:
                    print(f"  저장 전 {stock}: {data_dict[stock]}")
            
            saved_count += 1
        
        # 오늘 날짜 데이터는 수집했지만 저장하지 않는다고 표시
        if datetime.now().date() in new_data.index:
            print(f"\n== {today} 데이터는 수집했지만 저장하지 않습니다 ==")
            
        total_records = len(all_dates)
        print(f"총 {total_records}개 날짜 중 {saved_count}개가 처리되었습니다.")
        
        # ===== 추가: 데이터 업데이트 완료 후 기술적 지표 생성 및 뉴스 감정 분석 실행 =====
        # try:
        #     print("기술적 지표 생성 시작...")
        #     stock_service = StockRecommendationService()
        #     tech_result = stock_service.generate_technical_recommendations()
        #     print(f"기술적 지표 생성 완료: {tech_result['message']}")
            
        #     print("뉴스 감정 분석 시작...")
        #     sentiment_result = stock_service.fetch_and_store_sentiment_for_recommendations()
        #     print(f"뉴스 감정 분석 완료: {sentiment_result['message']}")
        # except Exception as sub_e:
        #     # 추가 작업 실패 시에도 원래 작업은 성공으로 간주
        #     print(f"추가 분석 작업 중 오류 발생: {str(sub_e)}")
        #     import traceback
        #     print(traceback.format_exc())
        
        return {
            "success": True,
            "message": "경제 데이터 업데이트 완료",
            "total_records": total_records,
            "updated_records": saved_count
        }
    except Exception as e:
        print(f"경제 데이터 업데이트 중 오류 발생: {str(e)}")
        import traceback
        print(traceback.format_exc())
        raise Exception(f"경제 데이터 업데이트 중 오류: {str(e)}")

print(f"Supabase URL: {settings.SUPABASE_URL}")