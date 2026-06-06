# Dashboards-as-code: grafonnet sources → JSON → ConfigMap manifest.
.PHONY: render apply deploy fmt clean

render:        ## render *.jsonnet → build/*.json + build/grafana-dashboards.configmap.yaml
	./render.sh

apply: render  ## render then apply the dashboards ConfigMap (rollout picks it up on next sync)
	kubectl apply -f build/grafana-dashboards.configmap.yaml

deploy: apply  ## apply the dashboards + (re)apply the Grafana stack itself
	kubectl apply -f ../k8s/95-grafana.yaml

fmt:           ## format the jsonnet sources in place
	jsonnetfmt -i lib/*.libsonnet dashboards/*.jsonnet

clean:
	rm -rf build vendor
