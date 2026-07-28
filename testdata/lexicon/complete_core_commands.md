# TCL 完整核心命令验证集

## 纳入规则

- `TCL 高频词` C:D：核心命令词。
- `TCL意图表（对比公版自由说）`：为定时/温度参数模板补充具体可执行实例及标准拼音。
- `DOA指令词`：需求 checklist 明确要求支持的 DOA 正向指令。

## 排除规则

- C:D 中的 `N` 参数模板字面量不播放；使用其具体展开实例。
- 温度实例仅纳入需求定义的 10.0 至 55.0 度（含 0.5 度步进）；范围外实例不作为正向验证词。
- `TCL 高频词` E:P 泛化词、`百万自由说`、意图表反集均不并入核心命令验证集。
- 词表汇总统计值不作为命令。

## 统计

- 完整可执行核心命令：489
- 缺失音频：0

| 来源类别 | 去重条数 |
| --- | ---: |
| high_frequency_core | 303 |
| mandatory_doa_core | 20 |
| parameter_instance_temperature | 91 |
| parameter_instance_timer | 25 |
| parameter_instance_timer_off | 25 |
| parameter_instance_timer_on | 25 |

- 输出：`complete_core_commands.csv`，每条命令含意图、标准语义、来源单元格和音频覆盖。
