# Diagnostics 测试夹具

仅用于隔离官方 Harness 的接线、身份、权限和故障验证，不安装到正式 profile，不访问真实用户数据。

从仓库根通过 pnpm verify 选择模块/gate，组合验证以 --fixtures 打包；本包不是业务插件，不持有生产运行时服务定义。
