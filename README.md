# 科研DAP2约束响应的坐标一致采集

此项目保存科学资料交换的原始文件、清单及冻结HTTP记录。`capture_data.io`读取JSON、核对资产身份，提供固定字节范围读取和顺序响应重放；`capture_data.domain`只核对资料类别、路径和数量，`capture_data.cli`输出概况；`capture_data.dap2`是受限DAP2采集适配器，让约束表达式、响应DDS声明与XDR解码共用同一个冻结请求。[资料约定](docs/data-contract.md)说明时间、字段与格式范围。所有站点、观测和机构资料均为合成，不连接航天器、接收机或真实研究系统。

Python3.11以上，无第三方Python依赖。项目根目录运行：

```sh
python -m unittest discover -s tests -v
python -m compileall -q capture_data tests
python -m capture_data.cli inspect data/normal.json
python -m capture_data.cli capture data/normal.json
```

`inspect`不解文件格式或选择HTTP版本，只显示资产身份及数量；`capture`按清单的冻结源尺寸验证`[start:stride:stop]`切片、生成白名单CE，经切块重放取回.dods后逐维核对约束后DDS并按XDR解码，输出CE、DDS原文摘要、每个元素的源索引、解码值与原响应字节范围。`invalid-identity.json`有预期资产摘要错误；wrong-shape的响应DDS维长与请求推导不符会整次失败；short-padding仅容忍响应末尾被截断的零padding，padding不进入值。同CE同版本可复用完整缓存，失败body不成为缓存命中，非200错误文本留档而不解释成空数组。`invalid-identity.json`有预期资产摘要错误，其余清单有合法来源身份但内容可含需领域操作识别的异常。`Source.read_at`记录请求范围，`Replay.next`记录实际method、URL与headers；`ReplayServer`在其上重放响应头并按任意切块读取body，协议与缓存策略由`Dap2Client`完成。

Dockerfile固定Linux基础镜像，`docker build -t science-material .`构建；`docker run --rm science-material`读取概况，`docker run --rm science-material python -m unittest discover -s tests -v`运行已有检查。资产均可离线读取，不需要下载数据或真实账号。
