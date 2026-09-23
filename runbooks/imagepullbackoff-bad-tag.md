# ImagePullBackOff: bad image tag

ImagePullBackOff or ErrImagePull means the kubelet cannot fetch the requested image. The usual causes are a misspelled repository or tag, a private registry without credentials, registry throttling, or an unavailable registry. Inspect `kubectl describe pod POD` and read the exact pull error from Events; do not infer the tag from the pod name.

Correct the image reference to a published immutable tag, configure an imagePullSecret for private registries, and restart the rollout with `kubectl rollout restart deployment NAME`. Wait for `kubectl rollout status` and confirm the pod is Ready. Do not automatically guess a replacement image tag because that can deploy an unintended version.

Apply/remove: apply `k8s/imagepullerror-deployment.yaml`; remove it with the matching `kubectl delete -f` command.
