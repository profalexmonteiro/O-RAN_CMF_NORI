#!/bin/bash

# Remove previous installations
helm uninstall influxdb -n influxdb
helm uninstall grafana -n grafana

# Install InfluxDB
kubectl create namespace influxdb 
kubectl create namespace grafana
helm repo add influxdata https://helm.influxdata.com/
helm repo add grafana https://grafana.github.io/helm-charts
helm upgrade --install influxdb influxdata/influxdb2 \
	--namespace influxdb \
	--set persistence.enabled=false \
	--set adminUser.username=admin \
	--set adminUser.password=admin1234 \
	--set adminUser.token=admin \
	--set adminUser.organization=openranbr \
	--set adminUser.bucket=openranbr \
	--set adminUser.retention_policy="1h" \
	--set service.type=NodePort \
	--set service.port=8086 \
	--set service.nodePort=30086 \
	--set persistence.size="1Gi"
helm upgrade --install grafana grafana/grafana \
	--namespace grafana \
	--version 6.56.4 \
	--set service.type=NodePort \
	--set service.port=3000 \
	--set service.nodePort=30085 \
	--set adminUser=admin \
	--set adminPassword=admin1234 \
	--set security.disableInitialAdminCreation=true \
	--set securityContext=null \
	--set podSecurityContext=null \
	--set containerSecurityContext=null \
	--set "extraEnvVars[0].name=GF_PATHS_DATA" \
	--set "extraEnvVars[0].value=/var/lib/grafana" \
	--set "extraEnvVars[1].name=GF_SECURITY_DISABLE_INITIAL_ADMIN_CREATION" \
	--set "extraEnvVars[1].value=true" \
	--set "datasources.datasources\\.yaml.apiVersion=1" \
	--set "datasources.datasources\\.yaml.datasources[0].name=InfluxDB-v2" \
	--set "datasources.datasources\\.yaml.datasources[0].type=influxdb" \
	--set "datasources.datasources\\.yaml.datasources[0].isDefault=true" \
	--set "datasources.datasources\\.yaml.datasources[0].url=http://influxdb-influxdb2.influxdb.svc.cluster.local:8086" \
	--set "datasources.datasources\\.yaml.datasources[0].access=proxy" \
	--set "datasources.datasources\\.yaml.datasources[0].jsonData.version=Flux" \
	--set "datasources.datasources\\.yaml.datasources[0].jsonData.organization=openranbr" \
	--set "datasources.datasources\\.yaml.datasources[0].jsonData.defaultBucket=openranbr" \
	--set "datasources.datasources\\.yaml.datasources[0].secureJsonData.token=admin" 