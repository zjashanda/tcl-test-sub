# TCL 需求词表支持范围审计

## 规则

- 正向支持词：`TCL意图表（对比公版自由说）` 非反集行、`TCL 高频词` 离线意图/核心命令词/泛化词、DOA 指令和 checklist 补充 DOA 词。
- 反集/误识别词：意图表中功能名含“反集”的行，以及 checklist 的唤醒反集。
- 参数模板：`N` 不可直接播报；以意图表中具体展开项作为实际验收词。
- 自由说：`百万自由说` 未并入本次离线词表验收范围。

## 统计

- 去重后正向支持文本：2558
- 去重后反集/不应识别文本：245
- 正反集合重叠文本：0
- 正向支持文本缺失音频：0

| 类别 | 去重文本数 |
| --- | ---: |
| canonical_intent | 325 |
| core_command | 307 |
| doa_command | 20 |
| generic_command | 2122 |
| offline_intent_definition | 156 |
| parameter_template_not_playable | 4 |
| wake_word | 2 |

## 输出文件

- `supported_lexicon.csv`：每个应支持文本的意图、标准语义、来源单元格、音频覆盖。
- `negative_lexicon.csv`：反集和不应识别文本。
- `parameter_expansions.csv`：定时/温度模板与意图表具体实例。
- `missing_audio.txt`：应支持但没有音频的文本。
- `positive_negative_overlap.txt`：同时出现于正向和反集的文本，需业务明确优先级。
