from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple

class BaseEvent(ABC):
    def __init__(self, event_id: str, start_time: str, end_time: str, 
                 name: str = "", description: str = "", metadata: Dict[str, Any] = None):
        self.event_id = event_id
        self.start_time = start_time
        self.end_time = end_time
        self.name = name
        self.description = description
        self.metadata = metadata or {}
        self._start_dt = None
        self._end_dt = None
    
    def get_start_datetime(self) -> datetime:
        if self._start_dt is None:
            # 注意：这里仅解析 HH:MM，日期部分默认为 1900-01-01
            # 实际仿真中由 Solver 赋予具体日期
            self._start_dt = datetime.strptime(self.start_time, "%H:%M")
        return self._start_dt
    
    def get_end_datetime(self) -> datetime:
        if self._end_dt is None:
            self._end_dt = datetime.strptime(self.end_time, "%H:%M")
        return self._end_dt
    
    def is_active_at(self, current_time: datetime) -> bool:
        """
        判断当前时间是否处于事件时间段内 (忽略日期，仅比较 HH:MM)
        """
        start = self.get_start_datetime()
        end = self.get_end_datetime()
        
        # 将 current_time 的日期替换为 start 的日期，以便只比较时间部分
        current_normalized = current_time.replace(year=start.year, month=start.month, day=start.day)
        
        if end < start:
            # 跨午夜事件 (例如 23:00 - 01:00)
            end_normalized = end + timedelta(days=1)
            # 如果当前时间小时数小于开始时间（说明是凌晨），加一天处理
            if current_normalized.hour < start.hour:
                current_normalized += timedelta(days=1)
            return start <= current_normalized < end_normalized
        else:
            # 正常日内事件
            return start <= current_normalized < end
    
    @abstractmethod
    def get_event_type(self) -> str:
        pass

    def get_fatigue_weight(self) -> float:
        """
        [新增] 获取该事件在计算连轴转疲劳时的时长折算权重。
        默认返回 1.0 (等同于标准课程)。
        """
        return 1.0
    
    @abstractmethod
    def calculate_stress_impact(self, user, current_stress: float, current_time: datetime) -> float:
        """
        旧接口：仅计算压力变化 (保留以兼容旧代码)
        """
        pass

    def calculate_stress_impact_dual(self, user, current_stress: float, current_energy: float, 
                                   current_time: datetime, time_step: int) -> Tuple[float, float]:
        """
        [新增] 新接口：双变量耦合计算 (压力 S, 精力 E)
        默认实现：调用旧接口计算 S，精力变化默认为 0
        子类 (CourseEvent, GymEvent 等) 必须覆盖此方法以实现具体逻辑
        
        Returns:
            (delta_S, delta_E)
        """
        delta_s = self.calculate_stress_impact(user, current_stress, current_time)
        delta_e = 0.0
        return delta_s, delta_e
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "type": self.get_event_type(),
            "name": self.name,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "description": self.description,
            "metadata": self.metadata
        }
