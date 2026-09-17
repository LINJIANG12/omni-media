"""OmniMedia-Ext: 配置文件驱动的外部模型音视频代读 MCP 服务。

与原生听音版 `omni-media-mcp` 的根本差异：本版本**不依赖宿主模型的音频模态**，
而是由服务自己按配置文件选定的外部端点（Gemini 协议 / OpenAI 协议）完成听音与
理解，把文本结果返回给调用方。
"""

from __future__ import annotations

__version__ = "0.2.0"

__all__ = ["__version__"]
