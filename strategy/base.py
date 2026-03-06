from abc import ABC, abstractmethod
from typing import Dict, Any

class BaseStrategy(ABC):
    def __init__(self, params: Dict[str, Any] = None):
        self.params = params or {}
    
    @abstractmethod
    def get_name(self) -> str:
        pass

