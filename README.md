# CollectProxySource

copy https://github.com/RenaLio/proxy-minging/

## 说明

Config.yaml	--- 爬取源

main.py --- 主程序

pre_check.py --- 运行前检查，主要检测输出的路径文件夹是否存在，(不存在->创建)

requirements.txt --- 依赖包

运行后会生成 `sub/merged.yaml`，这是汇总后的 Clash 配置文件。可将下面的 Raw 地址导入 Clash Verge：

`https://raw.githubusercontent.com/guipeichao/collectSub/main/sub/merged.yaml`

文件包含 `proxies`、`proxy-groups` 和默认 `MATCH` 规则。

