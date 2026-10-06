# 开发默认：叠 docker-compose.dev.yml（源码挂载 + KYLIN_DEV 热重载）
# 用法见 .cursor/rules/docker-dev-reload.mdc

COMPOSE := docker compose -f docker-compose.yml -f docker-compose.dev.yml

.PHONY: help up restart rebuild logs down ps

help:
	@echo "make up       - 启动/更新（叠 dev overlay；仅依赖变更时才真正 rebuild）"
	@echo "make restart  - 秒级重启 app（不重装 pip）"
	@echo "make rebuild  - 同 make up（显式命名，仍禁止 --no-cache）"
	@echo "make logs     - 跟踪 app 日志"
	@echo "make down     - 停止栈"
	@echo "make ps       - 查看容器状态"
	@echo ""
	@echo "改 Python 代码：保存即可热重载，不要 make restart / rebuild"

up:
	$(COMPOSE) up -d --build

restart:
	$(COMPOSE) restart app

rebuild: up

logs:
	$(COMPOSE) logs -f app

down:
	$(COMPOSE) down

ps:
	$(COMPOSE) ps
