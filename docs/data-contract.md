# DAP2 DDS与XDR子集

本源的数组维序为y,x，约束y[0:2:2]、x[1:2:3]各取两点。CE按变量、维序生成，不执行任意函数；返回DDS使用约束后尺寸，Grid ARRAY和每个MAP须一致。DDS标识符由ASCII字母下划线起首，其后可含数字；维度为命名非负整数，未知结构明确不支持。按DDS语法完成根Dataset后读换行Data:分隔符，数据负载里的Data:只是值。

本组XDR数值均网络序。标量Int16/UInt16使用32位槽，Int32/UInt32为32位，Float32/64为对应IEEE位型；String为u32字节长+原UTF8+补至4字节。数值数组先有u32元素数，再有XDR向量u32元素数，两者须相同；Byte向量同样有两个长度字，但负载为连续单字节并补至4；String数组只有一个元素数，随后逐项字符串。Byte标量为32位无符号槽。数组按末维最快，Grid按DDS声明顺序。尾部不得剩余数据。

本地response为完整.dods原字节，外层冻结版本视为服务方不可变版本证明；生产HTTP绑定请求及实际响应头，不从body推测新代。short-padding的原资产摘要有效，含最后字符串填充截断；wrong-shape要求采集操作发现DDS与CE不符。初始工具只读取原文件身份。

`capture_data.dap2`在上述子集上实现受限客户端：`FrozenRequest`是CE生成、响应DDS校验与XDR解码共用的唯一冻结事实。切片先对照冻结源尺寸验证（含stop、非负整数、stride>=1且可均匀取点），CE只投影白名单变量，不生成函数或服务端表达式；Grid的ARRAY与每个MAP按绑定维度同步切片，响应DDS的约束后维长逐维名称、顺序、长度核对，元素总数相同但轴不符同样拒绝。`Data:`边界由DDS文法解析得到，负载内的`Data:`只按字符串值处理。解码按DDS声明顺序进行，长度字不符、类型越界、非零padding、截断payload、尾部未解释字节均使整次请求失败；short-padding仅在padding为响应最后字节且在EOF处被截断时容忍，已存在的padding字节仍必须为零。成功结果保留CE、DDS原文摘要、每元素原索引/值/原响应字节范围；缓存键为(CE, source_version)，失败body不入缓存，非200响应的错误文本按server错误留档而非空数组。
