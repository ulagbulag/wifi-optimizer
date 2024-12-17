# Simple Wi-Fi Optimizer

A simple Wi-Fi connection optimizer based on the geolocation.
The connection's race condition can be resolved by matchmaking between the APs and the nodes.
The script runs on the nodes, because the AP's commercial loadbalancing features are nonfree and not a standard.

It's useful for highly congested environment such as Office with AI Training.
Currently only the static loadbalancing is supported.
Dynamic loadbalancing is in progress.

## Requirements

### sources.csv

```csv
kind,id,x,y
desktop,(my-machine-uuid),0,0
desktop,f1ba76c3-6c5b-4f19-bf8c-f71363052a9f,2,3
```

### targets.csv

```csv
kind,id,x,y
ap,(my-ap-mac-address),0,0
ap,12:34:56:78:9A:BC,3,4
```

## Test on the Local Machine

### With the docker

```bash
sudo docker run --privileged \
  --env DEBUG=true \
  --volume /run/dbus:/run/dbus:ro \
  --volume $(pwd):/src:ro \
  quay.io/ulagbulag/wifi-optimizer:latest
```

### Environment Variables

- DEBUG: Whether to show debug logs (default: false)
- SRC_FILE: The `sources.csv` file path (default: sources.csv)
- TGT_FILE: The `targets.csv` file path (default: targets.csv)
- DRY_RUN: Whether to change BSSID virtaully (default: false)
- INTERVAL_SECS: The interval of updating BSSID as seconds (default: 30)

## Deploy on the K8S

```bash
# Please edit examples/kubernetes/configmap.yaml to your own configuration

kubectl apply -f examples/kubernetes/namespace.yaml
kubectl apply -f examples/kubernetes/configmap.yaml
kubectl apply -f examples/kubernetes/daemonset.yaml
```

## License

Please check our [LICENSE](/LICENSE) file.
