"""后台任务(worker,架构方案第 4 节):采集与判定(collect)、建议与更新商机(suggest)、补全上传的线索(enrich)、
阶段变更记跟进(stage_log)。都经 core.tstore 读写新库,只填空、只新建、列表只追加(R2);本机只缓存可再生的东西(网页正文、已见条目)。
命令行:python -m core.cli run <任务> --profile …;常驻循环:python -m core.worker --profile …。"""
