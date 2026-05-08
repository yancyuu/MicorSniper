.PHONY: help build run run-debug dev clean logs pull push

# 默认目标
.DEFAULT_GOAL := help

# 变量定义 (从 .env 读取)
APP_NAME := $(shell grep -E '^APP__NAME=' .env 2>/dev/null | cut -d'=' -f2)
IMAGE_NAME := $(APP_NAME)
CONTAINER_NAME := $(APP_NAME)
CONTAINER_DEBUG_NAME := $(APP_NAME)-debug
# 自动检测平台，默认使用当前架构（可手动覆盖 make build PLATFORM=linux/amd64）
PLATFORM ?= linux/$(shell uname -m)
PORT := $(shell grep -E '^APP__PORT=' .env 2>/dev/null | cut -d'=' -f2)
DEBUG_PORT := $(shell grep -E '^APP__PORT=' .env 2>/dev/null | cut -d'=' -f2)

##@ 代码管理

pull: ## 拉取最新代码及子模块
	git pull origin main
	git submodule update --init --recursive
	git submodule update --remote --merge

##@ 常用命令

help: ## 显示帮助信息
	@echo "$(IMAGE_NAME) - Makefile 命令"
	@echo ""
	@echo "使用方法: make [target]"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

build: ## 构建 Docker 镜像
	DOCKER_BUILDKIT=1 docker build --platform $(PLATFORM) -t $(IMAGE_NAME):latest .

build-plain: ## 构建 Docker 镜像 (详细输出)
	DOCKER_BUILDKIT=1 BUILDKIT_PROGRESS=plain docker build --platform $(PLATFORM) -t $(IMAGE_NAME):latest .

run: ## 运行容器 (加载 .env)
	docker run -d -p $(PORT):$(PORT) \
		--env-file .env \
		--name $(CONTAINER_NAME) \
		$(IMAGE_NAME):latest

run-debug: ## 运行调试容器 (挂载代码目录，加载 .env)
	docker run -d \
		-p $(DEBUG_PORT):$(DEBUG_PORT) \
		-v $(PWD):/app \
		--env-file .env \
		-e APP__DEBUG=true \
		-e APP__AUTO_RELOAD=true \
		--name $(CONTAINER_DEBUG_NAME) \
		$(IMAGE_NAME):latest

dev: build run ## 构建并运行

dev-debug: build run-debug ## 构建并以调试模式运行

##@ 容器管理

stop: ## 停止运行中的容器
	docker stop $(CONTAINER_NAME) $(CONTAINER_DEBUG_NAME) 2>/dev/null || true

rm: stop ## 删除容器
	docker rm $(CONTAINER_NAME) $(CONTAINER_DEBUG_NAME) 2>/dev/null || true

restart: stop run ## 重启容器

logs: ## 查看容器日志
	docker logs -f $(CONTAINER_NAME)

logs-debug: ## 查看调试容器日志
	docker logs -f $(CONTAINER_DEBUG_NAME)

exec: ## 进入容器 shell
	docker exec -it $(CONTAINER_NAME) /bin/bash

##@ 清理

clean: rm ## 清理容器
	docker rmi $(IMAGE_NAME):latest 2>/dev/null || true

clean-all: clean ## 清理容器和镜像
	@echo "已清理所有相关资源"

##@ 发布

REGISTRY := registry.cn-shenzhen.aliyuncs.com/skgcom
VERSION ?= $(shell date +%Y%m%d)

push: ## 推送镜像到阿里云仓库 (make push VERSION=v1.1.8)
	docker tag $(IMAGE_NAME):latest $(REGISTRY)/$(IMAGE_NAME):$(VERSION)
	docker push $(REGISTRY)/$(IMAGE_NAME):$(VERSION)