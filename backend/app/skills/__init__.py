"""Agent 中台能力集合：Skills。

每个 Skill 对外是一个纯函数：
    skill.run(**kwargs) -> dict

对 Agent 层来说只看到 Skills 列表和 JSON Schema，不关心内部实现。
现阶段内部包装现有 mock service；未来迁 Agent 中台时只需换实现。
"""
