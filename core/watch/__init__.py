"""持续监测:信源定时采集 → 去重与变更检测 → 规则初筛 → 大模型判定与抽取 → 挂台账建议 → 系统内提醒与周报。

对照舆情监控:信源在 countries/{ISO3}/watch.yaml 里登记(公开信息),采集器按类型复用(core/watch/collectors.py);
线索(signals)系统自动写,台账(ledger)由人确认。设计与分工见 docs/SYSTEM_PLAN.md。
"""
