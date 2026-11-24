# 黔居采集台

这项服务用于采集合作方公开的旅居供给目录，核对住所、线路及交通、医疗、膳食、保险和语言配套，并把选定供给及其页面证据交接给渠道。仓库中的合作方页面和证明均为合成数据。

## 运行与验收

使用 Python 3.11 或更新版本。依赖已锁定在 `requirements.txt`。首次在本地搭建虚拟环境时可按锁定清单安装，预装环境可直接运行。服务面向本机可信操作员。用 `GUIZHOU_STAY_DB` 指定 SQLite 路径，默认使用 `guizhou_stay.sqlite3`。

```text
python -m pip install -r requirements.txt
python -m uvicorn guizhou_stay.api:from_environment --factory --host 127.0.0.1 --port 8000
python -m unittest discover -v
python -m tests.live_service
```

接口模型可从 `/openapi.json` 读取。验收在环回网络启动采集服务与合成合作方门户，实际读两页 HTML、复验原始证据、封存供给包，最后确认渠道回执。

## 采集与恢复

可以直接用目录回放合成页面。

```text
python -m guizhou_stay.cli --db demo.sqlite3 collect demo-capture fixtures/partner --limit 1
python -m guizhou_stay.cli --db demo.sqlite3 collect demo-capture fixtures/partner
python -m guizhou_stay.cli --db demo.sqlite3 audit demo-capture
```

也可启动 `guizhou_stay.local_portal:from_environment` 供主服务从 `POST /collections/{session_id}` 采集。HTTP 回放要求本机环回 URL。目录回放按 `first.html` 和后续游标命名文件。页面头用 `stay-source`、`stay-query`、`stay-revision`、`stay-locale`、`stay-page`、`stay-pages`、`stay-viewport`、`stay-observed`、`stay-cursor` 和 `stay-next` 元字段表明采集边界，空列表需要明示 `stay-empty=confirmed`。HTML 解析仅解释合成门户的静态标记，访问时无需脚本运行环境。

中断后继续使用原会话和原回放来源。后续页认领有有效期，短暂读取失败有次数上限与退避，过期工作不能入库。查询、语言、总页数或来源修订变化会终止会话并保留现场。末页与前页矛盾会阻止整份目录进入供给目录。原始 HTML、摘要、视口和结构化内容可从采集记录回查。审计会重新解析原始页面并核对目录使用的各个事实。

人工已经取得的结构化页面可以通过 `POST /sessions` 和 `POST /sessions/{session_id}/pages` 追加。页码及游标必须连续，新会话从第一页开始。人工记录也保存摘要，导出包会明确标识它只有结构化凭证。

## 选用证据并交接

目录在整个会话齐全后更新。身份按类型、提供方、标题、城市和地区归并。不同合作方若上报同一身份但在关键字段上相互冲突，覆盖评估会显示分歧及相关来源，冲突项会阻止当前类别通过。证明按来源、项目编号、类别及来源修订绑定，有效期起点包含、终点不包含。查询时间必须包含时区。

`/plans` 可以为一个地区预登记必采和可选查询。按计划发布时，可为每个查询选择完整会话，并查看启用代次和历史。晚收、更换会话或证明变化会影响覆盖。预览返回摘要。发布可提交该摘要及预期版次，检查预览和提交之间是否发生变化。只有完整采集覆盖和供给证明达到计划时，封存包才会标为已发布。

封存 JSON 和 ZIP 读取的是保存的同一版内容。ZIP 保存页面原件、依据 CSV 和完整性清单，可用 `python -m guizhou_stay.cli verify-bundle` 离线核验。对渠道交接时，`/deliveries` 登记指定版次。渠道回执确认摘要后才会接受下一次出件。重复交接请求仍指向原出件编号和原封存内容。
