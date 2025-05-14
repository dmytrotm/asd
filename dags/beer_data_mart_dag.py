from datetime import datetime, timedelta
import json
import os
from contextlib import contextmanager

from airflow.hooks.base import BaseHook
import pandas as pd
from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.models import Variable

# Константи
dataset_folder_path = json.loads(BaseHook.get_connection('dataset_folder').extra)['path']
POSTGRES_CONN_ID = 'postgres_conn'  # З'єднання з PostgreSQL

# Налаштування DAG
default_args = {
    'owner': 'airflow',
    'depends_on_past': False,
    'start_date': datetime(2025, 4, 10),
    'email_on_failure': False,
    'email_on_retry': False,
    'retries': 5,
    'retry_delay': timedelta(seconds=30),
}

dag = DAG(
    'beer_data_mart_processing',
    default_args=default_args,
    description='Pipeline для побудови вітрин даних про пиво',
    schedule=timedelta(days=1),
)

# Контекстний менеджер для безпечної роботи з базою даних
@contextmanager
def get_postgres_connection():
    pg_hook = PostgresHook(postgres_conn_id=POSTGRES_CONN_ID)
    conn = pg_hook.get_conn()
    conn.autocommit = False
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

# КРОК 1: Створення партиційної таблиці за стилем пива
# Це дозволить секціонувати дані по категоріях пива
create_style_partitioned_table = SQLExecuteQueryOperator(
    task_id='create_style_partitioned_table',
    conn_id=POSTGRES_CONN_ID,
    sql="""
    -- Спочатку створимо таблицю для партицій
    CREATE TABLE IF NOT EXISTS partitioned_beer_data (
        beer_id INTEGER,
        style_id INTEGER,
        brewery_id INTEGER,
        style_name VARCHAR(255),
        beer_name VARCHAR(255),
        brewery_name VARCHAR(255),
        abv FLOAT,
        min_ibu FLOAT,
        max_ibu FLOAT,
        astringency FLOAT,
        body FLOAT,
        alcohol FLOAT,
        bitter FLOAT,
        sweet FLOAT,
        sour FLOAT,
        salty FLOAT,
        fruits FLOAT,
        hoppy FLOAT,
        spices FLOAT,
        malty FLOAT,
        review_aroma NUMERIC(3,2),
        review_appearance NUMERIC(3,2),
        review_palate NUMERIC(3,2),
        review_taste NUMERIC(3,2),
        average_review_overall NUMERIC(3,2),
        number_of_reviews INTEGER,
        last_updated TIMESTAMP,
        PRIMARY KEY (style_id, beer_id)
    ) PARTITION BY LIST (style_id);
    """,
    dag=dag,
)

# КРОК 1.5: Очищення партиційної таблиці перед створенням партицій
# Це допоможе уникнути проблем з дублікатами
clear_partitioned_table = SQLExecuteQueryOperator(
    task_id='clear_partitioned_table',
    conn_id=POSTGRES_CONN_ID,
    sql="""
    -- Очищаємо партиційну таблицю перед подальшою обробкою
    TRUNCATE TABLE partitioned_beer_data;
    """,
    dag=dag,
)

# Функція для створення партицій для кожного стилю пива
def create_style_partitions(**kwargs):
    with get_postgres_connection() as conn:
        cursor = conn.cursor()
        
        # Отримуємо всі унікальні style_id з таблиці dim_style
        cursor.execute("SELECT style_id, style_name FROM dim_style ORDER BY style_id")
        styles = cursor.fetchall()
        
        # Для кожного стилю створюємо окрему партицію
        for style_id, style_name in styles:
            safe_style_name = f"style_{style_id}"
            
            # Перевіряємо, чи вже існує партиція для цього стилю
            cursor.execute(f"""
                SELECT count(*) FROM pg_class 
                WHERE relname = 'partitioned_beer_data_{safe_style_name}'
            """)
            
            if cursor.fetchone()[0] == 0:
                try:
                    # Створюємо партицію для цього стилю
                    cursor.execute(f"""
                        CREATE TABLE IF NOT EXISTS partitioned_beer_data_{safe_style_name}
                        PARTITION OF partitioned_beer_data 
                        FOR VALUES IN ({style_id});
                    """)
                    print(f"Створено партицію для стилю {style_id}: {style_name}")
                except Exception as e:
                    print(f"Помилка при створенні партиції для стилю {style_id}: {e}")
        
        # Заповнюємо партиційну таблицю даними - використовуємо ON CONFLICT DO NOTHING для уникнення помилок з дублікатами
        cursor.execute("""
            -- Заповнюємо партиційну таблицю актуальними даними
            INSERT INTO partitioned_beer_data
            SELECT 
                f.beer_id,
                f.style_id,
                f.brewery_id,
                s.style_name,
                b.beer_name,
                br.brewery_name,
                f.abv,
                f.min_ibu,
                f.max_ibu,
                f.astringency,
                f.body,
                f.alcohol,
                f.bitter,
                f.sweet,
                f.sour,
                f.salty,
                f.fruits,
                f.hoppy,
                f.spices,
                f.malty,
                f.review_aroma,
                f.review_appearance,
                f.review_palate,
                f.review_taste,
                f.average_review_overall,
                f.number_of_reviews,
                f.last_updated
            FROM fact_beer f
            JOIN dim_beer b ON f.beer_id = b.beer_id
            JOIN dim_style s ON f.style_id = s.style_id
            JOIN dim_brewery br ON f.brewery_id = br.brewery_id
            WHERE f.is_active = TRUE
            ON CONFLICT (style_id, beer_id) DO NOTHING;  -- Додаємо обробку конфліктів, щоб уникнути помилок з дублікатами
        """)
        
        # Перевіряємо кількість записів у партиційній таблиці
        cursor.execute("SELECT COUNT(*) FROM partitioned_beer_data")
        count = cursor.fetchone()[0]
        print(f"Заповнено партиційну таблицю. Загальна кількість записів: {count}")
        
        # Підраховуємо кількість записів у кожній партиції
        cursor.execute("""
            SELECT s.style_name, COUNT(*) 
            FROM partitioned_beer_data p
            JOIN dim_style s ON p.style_id = s.style_id
            GROUP BY s.style_name
            ORDER BY COUNT(*) DESC
        """)
        
        partition_counts = cursor.fetchall()
        print("Розподіл даних по партиціях:")
        for style_name, count in partition_counts:
            print(f"  - {style_name}: {count} записів")
    
    return True

# КРОК 2: Створення вітрин даних (data marts)

# Вітрина 1: Аналіз середніх оцінок пива за стилями та пивоварнями
create_ratings_by_style_mart = SQLExecuteQueryOperator(
    task_id='create_ratings_by_style_mart',
    conn_id=POSTGRES_CONN_ID,
    sql="""
    -- Створюємо або оновлюємо вітрину даних
    DROP TABLE IF EXISTS mart_ratings_by_style;

    CREATE TABLE mart_ratings_by_style AS
    SELECT
        style_id,
        style_name,
        AVG(average_review_overall) AS avg_overall_rating,
        AVG(review_aroma) AS avg_aroma_rating,
        AVG(review_appearance) AS avg_appearance_rating,
        AVG(review_palate) AS avg_palate_rating,
        AVG(review_taste) AS avg_taste_rating,
        SUM(number_of_reviews) AS total_reviews,
        COUNT(beer_id) AS beer_count,
        CAST(AVG(abv) AS NUMERIC(5,2)) AS avg_abv,
        CAST(AVG(min_ibu) AS NUMERIC(5,2)) AS avg_min_ibu,
        CAST(AVG(max_ibu) AS NUMERIC(5,2)) AS avg_max_ibu,
        DATE_TRUNC('month', MAX(last_updated))::date AS data_month,
        CURRENT_TIMESTAMP AS created_at
    FROM partitioned_beer_data
    GROUP BY style_id, style_name
    ORDER BY avg_overall_rating DESC;

    -- Додаємо індекси для оптимізації запитів
    CREATE INDEX IF NOT EXISTS idx_style_mart_rating ON mart_ratings_by_style(avg_overall_rating);
    CREATE INDEX IF NOT EXISTS idx_style_mart_id ON mart_ratings_by_style(style_id);
    """,
    dag=dag,
)

# Вітрина 2: Аналіз характеристик пива за пивоварнями 
create_brewery_profile_mart = SQLExecuteQueryOperator(
    task_id='create_brewery_profile_mart',
    conn_id=POSTGRES_CONN_ID,
    sql="""
    -- Створюємо або оновлюємо вітрину даних
    DROP TABLE IF EXISTS mart_brewery_profile;

    CREATE TABLE mart_brewery_profile AS
    SELECT
        brewery_id,
        brewery_name,
        COUNT(beer_id) AS beer_count,
        COUNT(DISTINCT style_id) AS style_variety,
        CAST(AVG(abv) AS NUMERIC(5,2)) AS avg_abv,
        CAST(AVG(bitter) AS NUMERIC(5,4)) AS avg_bitter_profile,
        CAST(AVG(sweet) AS NUMERIC(5,4)) AS avg_sweet_profile,
        CAST(AVG(sour) AS NUMERIC(5,4)) AS avg_sour_profile,
        CAST(AVG(fruits) AS NUMERIC(5,4)) AS avg_fruits_profile,
        CAST(AVG(hoppy) AS NUMERIC(5,4)) AS avg_hoppy_profile,
        CAST(AVG(malty) AS NUMERIC(5,4)) AS avg_malty_profile,
        CAST(AVG(average_review_overall) AS NUMERIC(5,2)) AS avg_rating,
        SUM(number_of_reviews) AS total_reviews,
        DATE_TRUNC('month', MAX(last_updated))::date AS data_month,
        CURRENT_TIMESTAMP AS created_at
    FROM partitioned_beer_data
    GROUP BY brewery_id, brewery_name
    ORDER BY avg_rating DESC;

    -- Додаємо індекси для оптимізації запитів
    CREATE INDEX IF NOT EXISTS idx_brewery_mart_rating ON mart_brewery_profile(avg_rating);
    CREATE INDEX IF NOT EXISTS idx_brewery_mart_beers ON mart_brewery_profile(beer_count);
    """,
    dag=dag,
)

# Вітрина 3: Періодичний аналіз топ-пива за характеристиками
create_top_beers_mart = SQLExecuteQueryOperator(
    task_id='create_top_beers_mart',
    conn_id=POSTGRES_CONN_ID,
    sql="""
    -- Створюємо або оновлюємо вітрину даних
    DROP TABLE IF EXISTS mart_top_beers;
    
    CREATE TABLE mart_top_beers AS
    WITH ranked_beers AS (
        SELECT 
            beer_id,
            beer_name,
            style_id,
            style_name,
            brewery_id,
            brewery_name,
            abv,
            average_review_overall,
            number_of_reviews,
            RANK() OVER (ORDER BY average_review_overall DESC, number_of_reviews DESC) AS rank_by_rating,
            RANK() OVER (ORDER BY abv DESC) AS rank_by_abv,
            RANK() OVER (ORDER BY bitter DESC) AS rank_by_bitter,
            RANK() OVER (ORDER BY sweet DESC) AS rank_by_sweet,
            RANK() OVER (ORDER BY hoppy DESC) AS rank_by_hoppy,
            DATE_TRUNC('month', last_updated)::date AS data_month
        FROM partitioned_beer_data
        WHERE number_of_reviews >= 5  -- Мінімальна кількість відгуків для достовірності
    )
    SELECT 
        beer_id,
        beer_name,
        style_id,
        style_name,
        brewery_id,
        brewery_name,
        abv,
        average_review_overall,
        number_of_reviews,
        rank_by_rating,
        rank_by_abv,
        rank_by_bitter,
        rank_by_sweet,
        rank_by_hoppy,
        data_month,
        CURRENT_TIMESTAMP AS created_at
    FROM ranked_beers
    WHERE 
        rank_by_rating <= 50 OR  -- Топ-50 за рейтингом
        rank_by_abv <= 20 OR     -- Топ-20 за вмістом алкоголю 
        rank_by_bitter <= 20 OR   -- Топ-20 за гіркістю
        rank_by_sweet <= 20 OR    -- Топ-20 за солодкістю
        rank_by_hoppy <= 20       -- Топ-20 за хмелевістю
    ORDER BY rank_by_rating;
    
    -- Додаємо індекси для оптимізації запитів
    CREATE INDEX IF NOT EXISTS idx_top_beers_rating ON mart_top_beers(rank_by_rating);
    CREATE INDEX IF NOT EXISTS idx_top_beers_beer_id ON mart_top_beers(beer_id);
    """,
    dag=dag,
)

# Функція для експорту вітрин у JSON-файли
def export_datamarts_to_json(**kwargs):
    with get_postgres_connection() as conn:
        cursor = conn.cursor()
        
        # Список вітрин для експорту
        datamarts = [
            "mart_ratings_by_style",
            "mart_brewery_profile",
            "mart_top_beers"
        ]
        
        for mart_name in datamarts:
            try:
                # Отримуємо дані з вітрин
                cursor.execute(f"SELECT * FROM {mart_name}")
                rows = cursor.fetchall()
                columns = [desc[0] for desc in cursor.description]
                
                # Конвертуємо у DataFrame
                df = pd.DataFrame(rows, columns=columns)
                
                # Зберігаємо як JSON
                export_path = os.path.join(dataset_folder_path, f"{mart_name}.json")
                df.to_json(export_path, orient='records', date_format='iso', indent=4)
                
                print(f"Експортовано {len(df)} рядків з {mart_name} у файл {export_path}")
                
                # Виводимо зразок даних для перевірки
                print(f"Зразок даних з {mart_name}:")
                print(df.head(2).to_dict('records'))
                
            except Exception as e:
                print(f"Помилка при експорті вітрини {mart_name}: {e}")
    
    return True

# Зберігаємо статистику використання вітрин
def log_datamart_statistics(**kwargs):
    with get_postgres_connection() as conn:
        cursor = conn.cursor()
        
        # Створюємо таблицю логів, якщо вона не існує
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS datamart_statistics (
            id SERIAL PRIMARY KEY,
            mart_name VARCHAR(255),
            row_count INTEGER,
            size_bytes BIGINT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        
        # Список вітрин для аналізу
        datamarts = [
            "mart_ratings_by_style",
            "mart_brewery_profile", 
            "mart_top_beers"
        ]
        
        for mart_name in datamarts:
            # Отримуємо кількість рядків
            cursor.execute(f"SELECT COUNT(*) FROM {mart_name}")
            row_count = cursor.fetchone()[0]
            
            # Отримуємо розмір вітрини
            cursor.execute(f"""
                SELECT pg_total_relation_size('{mart_name}')
            """)
            size_bytes = cursor.fetchone()[0]
            
            # Зберігаємо статистику
            cursor.execute("""
                INSERT INTO datamart_statistics (mart_name, row_count, size_bytes)
                VALUES (%s, %s, %s)
            """, (mart_name, row_count, size_bytes))
            
            print(f"Статистика вітрини {mart_name}: {row_count} рядків, {size_bytes} байт")
    
    return True

# Оператори для експорту та статистики
export_datamarts = PythonOperator(
    task_id='export_datamarts',
    python_callable=export_datamarts_to_json,
    dag=dag,
)

log_statistics = PythonOperator(
    task_id='log_statistics',
    python_callable=log_datamart_statistics,
    dag=dag,
)

# Визначаємо оператор для створення партицій
create_partitions = PythonOperator(
    task_id='create_partitions',
    python_callable=create_style_partitions,
    dag=dag,
)

# Послідовність виконання завдань
create_style_partitioned_table >> clear_partitioned_table >> create_partitions
create_partitions >> create_ratings_by_style_mart
create_partitions >> create_brewery_profile_mart
create_partitions >> create_top_beers_mart
[create_ratings_by_style_mart, create_brewery_profile_mart, create_top_beers_mart] >> export_datamarts >> log_statistics