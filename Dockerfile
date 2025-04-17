FROM apache/airflow:2.10.5
USER root
RUN pip install scikit-learn
USER airflow