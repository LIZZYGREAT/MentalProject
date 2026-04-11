from abc import ABC, abstractmethod
from typing import Dict, Any

class BaseStrategy(ABC):
    """策略基类：params 为 GLOBAL_DEFAULT_CONFIG 中与该策略相关的子集。"""
    def __init__(self, params: Dict[str, Any] = None):
        self.params = params or {}
    
    @abstractmethod
    def get_name(self) -> str:
        pass

