"""Agent 可调用的工具集合。

**一个工具一个文件**，统一契约见 ``app/tools/base.py``：

    NAME / DESCRIPTION / PARAMETERS / CATEGORY / IS_READ_ONLY /
    APPLICABLE_STATES / run()

分三类：
    FACT      只读事实：查目录、指标、能力、当前草稿、当前区块、校验结果
    BUILDER   确定性组装：按计划组装草稿
    TERMINAL  终结工具：提交本轮结构化结论

工具后续会上线到 skill 中台，因此契约和实现分离：
    - 契约（名字 / 描述 / 入参 schema / 权限语义）随包导出，见
      ``registry.export_manifest()``；
    - 实现（``run()``）内部包 mock service，迁中台时只换实现不改契约。
"""
from __future__ import annotations
