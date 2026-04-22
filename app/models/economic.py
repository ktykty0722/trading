from datetime import date
from typing import Optional, Dict, Any

class EconomicData:
    """경제 및 주식 데이터 모델"""
    
    def __init__(self, 날짜: date, **kwargs):
        self.날짜 = 날짜
        
        # 동적으로 속성 할당
        for key, value in kwargs.items():
            setattr(self, key, value)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]):
        """딕셔너리에서 모델 인스턴스 생성"""
        return cls(**data)
    
    def to_dict(self) -> Dict[str, Any]:
        """모델 인스턴스를 딕셔너리로 변환"""
        return {k: v for k, v in self.__dict__.items() if v is not None}