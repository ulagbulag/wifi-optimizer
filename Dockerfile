# Copyright (c) 2024 Ho Kim (ho.kim@ulagbulag.io). All rights reserved.
# Use of this source code is governed by a GPL-3-style license that can be
# found in the LICENSE file.

FROM docker.io/library/python:3.13-slim

# Install dependencies
RUN apt-get update && apt-get install -y \
    dmidecode \
    # Cleanup
    && apt-get clean all \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
# - sdbus: used directly for the wpa_supplicant (fi.w1.wpa_supplicant1) D-Bus
#   proxies of the `wpa` backend; declared explicitly so it is not relied upon
#   only as a transitive dependency.
# - sdbus-networkmanager: NetworkManager D-Bus client (the `nm` backend).
RUN pip install --only-binary ':all:' sdbus sdbus-networkmanager && \
    pip install pandas

# Upload the script
ADD ./wifi_optimizer.py /usr/local/bin/wifi_optimizer.py

# Server Configuration
ENV BACKEND="nm"
ENV DEBUG="false"
ENV INTERVAL_SECS="30"
ENV SRC_FILE="/src/sources.csv"
ENV TGT_FILE="/src/targets.csv"
CMD [ "/usr/local/bin/wifi_optimizer.py" ]
