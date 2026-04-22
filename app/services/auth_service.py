def parse_expiration_date(date_str):
    try:
        # 정규 표현식으로 마이크로초 부분 처리
        import re
        if isinstance(date_str, str) and re.search(r'\.\d{5}\+', date_str):  # 5자리 소수점 확인
            # 마이크로초 부분을 6자리로 맞추기 - 수정된 부분
            date_str = re.sub(r'\.(\d{5})\+', r'.\g<1>0+', date_str)
        
        # datetime 직접 사용
        from datetime import datetime
        import pytz
        
        if isinstance(date_str, str):
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S.%f%z")
                return dt
            except ValueError:
                # 다른 형식도 시도
                try:
                    dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
                    # 시간대 정보 추가
                    return dt.replace(tzinfo=pytz.UTC)
                except:
                    pass
        # 이미 datetime 객체인 경우
        return date_str
    except Exception as e:
        print(f"날짜 파싱 오류: {e}")
        # 현재 시간 + 1일을 기본값으로 반환 - 시간대 정보 추가
        from datetime import datetime, timedelta
        import pytz
        return datetime.now(pytz.UTC) + timedelta(days=1) 