ARG CASES_IMAGE=evalhive-benchmark-cases:0.1.0
FROM ${CASES_IMAGE}
COPY benchmarks/toolathlon-gym/src /opt/toolathlon-package
ENV PYTHONPATH=/opt/benchmark-sdk:/opt/native-package:/opt/toolathlon-package
CMD ["python", "-m", "evalhive_toolathlon.worker"]
