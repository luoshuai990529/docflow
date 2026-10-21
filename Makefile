.PHONY: start stop

start:
	docker compose up -d --build --wait
	@address=$$(docker compose port web 8080); \
	if [ -z "$$address" ]; then \
		echo "无法获取 Web 服务的本地端口" >&2; \
		exit 1; \
	fi; \
	printf '\n本地访问地址：http://%s\n' "$$address"

stop:
	docker compose stop
