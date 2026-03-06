from typing import Dict, List, Callable, Any
from event.base import BaseEvent

class EventBus:
    def __init__(self):
        self._listeners: Dict[str, List[Callable]] = {}
        self._event_history: List[BaseEvent] = []
    
    def subscribe(self, event_type: str, handler: Callable[[BaseEvent, Any], None]):
        if event_type not in self._listeners:
            self._listeners[event_type] = []
        self._listeners[event_type].append(handler)
    
    def unsubscribe(self, event_type: str, handler: Callable):
        if event_type in self._listeners:
            if handler in self._listeners[event_type]:
                self._listeners[event_type].remove(handler)
    
    def publish(self, event: BaseEvent, context: Any = None):
        self._event_history.append(event)
        event_type = event.get_event_type()
        if event_type in self._listeners:
            for handler in self._listeners[event_type]:
                try:
                    handler(event, context)
                except Exception as e:
                    print(f"Error in event handler: {e}")
    
    def get_event_history(self) -> List[BaseEvent]:
        return self._event_history.copy()
    
    def clear_history(self):
        self._event_history.clear()

