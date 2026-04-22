from pydantic import Field
from pydantic_settings import BaseSettings
from typing import List, Optional, Union, Literal, get_type_hints
import os
from dotenv import load_dotenv

# .env 파일 로드
load_dotenv()

class Settings(BaseSettings):
    PROJECT_NAME: str = "주식 분석 API"
    PROJECT_DESCRIPTION: str = "해외주식 잔고 조회 및 주식 예측 API"
    PROJECT_VERSION: str = "1.0.0"

    # DEBUG 설정 추가
    DEBUG: bool = Field(default=False, description="디버그 모드 활성화 여부")

    CORS_ORIGINS: List[str] = ["*"]

    SUPABASE_URL: str = os.getenv("SUPABASE_URL")
    SUPABASE_KEY: str = os.getenv("SUPABASE_KEY")

    # 한국투자증권 API 설정
    KIS_USE_MOCK: bool = Field(default=True, description="모의투자 사용 여부")

    KIS_BASE_URL: str = Field(
        default="https://openapivts.koreainvestment.com:29443",
        description="한국투자증권 API 기본 URL (모의투자용)"
    )
    KIS_REAL_URL: str = Field(
        default="https://openapi.koreainvestment.com:9443",
        description="한국투자증권 API 기본 URL (실제투자용)"
    )

    # 모의투자 계좌 정보
    KIS_MOCK_APPKEY: str = Field(default="", description="모의투자 앱키")
    KIS_MOCK_APPSECRET: str = Field(default="", description="모의투자 앱시크릿")
    KIS_MOCK_CANO: str = Field(default="50173046", description="모의투자 계좌번호")

    # 실제투자 계좌 정보
    KIS_REAL_APPKEY: str = Field(default="", description="실제투자 앱키")
    KIS_REAL_APPSECRET: str = Field(default="", description="실제투자 앱시크릿")
    KIS_REAL_CANO: str = Field(default="64856431", description="실제투자 계좌번호")

    # .env 호환용 (직접 사용하지 않고 property로 대체)
    KIS_APPKEY: str = Field(default="", description="한국투자증권 API 앱키")
    KIS_APPSECRET: str = Field(default="", description="한국투자증권 API 앱시크릿")
    KIS_CANO: str = Field(default="", description="계좌번호 앞 8자리")
    KIS_ACNT_PRDT_CD: str = Field(default="01", description="계좌번호 뒤 2자리")

    ALPHA_VANTAGE_API_KEY: str = os.getenv("ALPHA_VANTAGE_API_KEY", "")
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    TR_ID: str = os.getenv("TR_ID")

    @property
    def kis_base_url(self) -> str:
        """사용할 한국투자증권 API URL 반환"""
        return self.KIS_BASE_URL if self.KIS_USE_MOCK else self.KIS_REAL_URL

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # KIS_USE_MOCK에 따라 활성 계좌 정보 자동 전환
        if self.KIS_USE_MOCK:
            if self.KIS_MOCK_APPKEY:
                self.KIS_APPKEY = self.KIS_MOCK_APPKEY
            if self.KIS_MOCK_APPSECRET:
                self.KIS_APPSECRET = self.KIS_MOCK_APPSECRET
            if self.KIS_MOCK_CANO:
                self.KIS_CANO = self.KIS_MOCK_CANO
        else:
            if self.KIS_REAL_APPKEY:
                self.KIS_APPKEY = self.KIS_REAL_APPKEY
            if self.KIS_REAL_APPSECRET:
                self.KIS_APPSECRET = self.KIS_REAL_APPSECRET
            if self.KIS_REAL_CANO:
                self.KIS_CANO = self.KIS_REAL_CANO

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True

# 싱글톤 설정 객체 생성
settings = Settings()