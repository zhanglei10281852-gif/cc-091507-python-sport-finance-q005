# 运动康复费用理赔账本

面向运动员治疗费用、保险责任和俱乐部福利报销的 Python 后端服务。

当前服务提供标准库 HTTP 运行入口、健康检查和领域参考资料，业务数据目录预留为 `reference/`。接口扩展应保持 JSON 响应，并将持久化文件写入 `.runtime/`。

## 运行

需要 Python 3.11 或更高版本：

```bash
python3 src/index.py
```

服务默认监听 `8000` 端口，访问 `GET /health` 可确认进程状态。执行测试：

```bash
python3 -m unittest discover -s tests
```

也可以运行 `docker compose up --build` 启动容器。
