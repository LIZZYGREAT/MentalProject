from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple

class BaseEvent(ABC):
    """单日日程中一条事件的抽象基类；仿真器只关心时间段与双变量冲击接口。"""
    def __init__(self, event_id: str, start_time: str, end_time: str, 
                 name: str = "", description: str = "", metadata: Dict[str, Any] = None):
        """
        参数:
            event_id: 事件唯一键；start_time/end_time: 含日期的时刻字符串或可被解析的时间；
            name/description: 展示与情感打分；metadata: 子类扩展字段（如 idle_duration、credits）。
        """
        self.event_id = event_id
        self.start_time = start_time
        self.end_time = end_time
        self.name = name
        self.description = description
        self.metadata = metadata or {}
        self._start_dt = None
        self._end_dt = None
    
    def get_start_datetime(self) -> datetime:
        """解析 start_time 中 HH:MM 为 datetime（仅时间轴比较用，仿真里由外层拼真实日期）。"""
        if self._start_dt is None:
            self._start_dt = datetime.strptime(self.start_time, "%H:%M")
        return self._start_dt
    
    def get_end_datetime(self) -> datetime:
        """同 get_start_datetime，解析结束时刻。"""
        if self._end_dt is None:
            self._end_dt = datetime.strptime(self.end_time, "%H:%M")
        return self._end_dt
    
    def is_active_at(self, current_time: datetime) -> bool:
        """判断 current_time 是否落在 [start, end)（支持跨午夜）。"""
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
        """连续负荷时长加权；课程/任务为正，运动可为负用于冷却累计。"""
        return 1.0
    
    @abstractmethod
    def calculate_stress_impact(self, user, current_stress: float, current_time: datetime) -> float:
        """仅返回压力增量 dS（兼容旧调用）；新逻辑请用 calculate_stress_impact_dual。"""
        pass

    def calculate_stress_impact_dual(self, user, current_stress: float, current_energy: float, 
                                   current_time: datetime, time_step: int) -> Tuple[float, float]:
        """
        计算本积分步的压力、精力增量。子类应覆盖。
        参数:
            user: User（读参数与策略）；current_stress/current_energy: 当前 S、E；
            current_time: 仿真时刻；time_step: 步长（分钟）。
        返回:
            (delta_S, delta_E)。默认实现仅调 calculate_stress_impact 且 dE=0。
        """
        delta_s = self.calculate_stress_impact(user, current_stress, current_time)
        delta_e = 0.0
        return delta_s, delta_e
    
    def to_dict(self) -> Dict[str, Any]:
        """序列化为前端/API 可用的扁平字典。"""
        item = {
            "event_id": self.event_id,
            "type": self.get_event_type(),
            "name": self.name,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "description": self.description,
            "metadata": self.metadata
        }
        if hasattr(self, "task_type"):
            item["task_type"] = getattr(self, "task_type")
        return item
