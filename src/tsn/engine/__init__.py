"""engine —— 轨迹引擎.

与协议版本无关：输入「目标距离 + 起点 + 规则」，输出「一条自洽的轨迹」。
adapter 换版不影响这里。
"""

from tsn.engine import noise, physics, steps, track, validate

__all__ = ["noise", "physics", "steps", "track", "validate"]
