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
    'start_date': datetime(2025, 4, 11),
    'email_on_failure': False,
    'email_on_retry': False,
    'retries': 5,
    'retry_delay': timedelta(seconds=30),
}

dag = DAG(
    'beer_time_partitioning',
    default_args=default_args,
    description='Pipeline для створення партицій даних по часу',
    schedule=timedelta(days=28),  # Виконується раз на 4 тижні
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

# Створення таблиці з партиціонування по часу
create_time_partitioned_beer_ratings = SQLExecuteQueryOperator(
    task_id='create_time_partitioned_beer_ratings',
    conn_id=POSTGRES_CONN_ID,
    sql="""
    -- Перевіряємо, чи існує таблиця
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT FROM pg_tables 
            WHERE tablename = 'beer_ratings_history'
        ) THEN
            -- Створюємо таблицю для партицій по часу
            CREATE TABLE beer_ratings_history (
                record_id SERIAL,
                beer_id INTEGER,
                beer_name VARCHAR(255),
                style_id INTEGER,
                style_name VARCHAR(255),
                brewery_id INTEGER, 
                brewery_name VARCHAR(255),
                average_review_overall NUMERIC(3,2),
                number_of_reviews INTEGER,
                recorded_month DATE,
                recorded_at TIMESTAMP NOT NULL,
                PRIMARY KEY (record_id, recorded_month)
            ) PARTITION BY RANGE (recorded_month);
            
            -- Створюємо індекси
            CREATE INDEX IF NOT EXISTS idx_ratings_beer_id 
            ON beer_ratings_history(beer_id);
            
            CREATE INDEX IF NOT EXISTS idx_ratings_style_id 
            ON beer_ratings_history(style_id);
            
            CREATE INDEX IF NOT EXISTS idx_ratings_recorded 
            ON beer_ratings_history(recorded_month);
        END IF;
    END
    $$;
    """,
    dag=dag,
)

# Функція для створення партицій за часом
def create_time_partitions(**kwargs):
    with get_postgres_connection() as conn:
        cursor = conn.cursor()
        
        # Отримуємо поточну дату
        current_date = datetime.now().date()
        
        # Створюємо партиції для минулих місяців та на рік вперед
        months_back = 6  # Скільки місяців назад почати
        months_forward = 12  # Скільки місяців вперед створити партиції
        
        for i in range(-months_back, months_forward + 1):
            month_date = (current_date.replace(day=1) + timedelta(days=32 * i)).replace(day=1)
            next_month = (month_date + timedelta(days=32)).replace(day=1)
            
            partition_name = f"beer_ratings_{month_date.strftime('%Y_%m')}"
            
            # Перевіряємо, чи вже існує партиція для цього місяця
            cursor.execute(f"""
                SELECT count(*) FROM pg_class 
                WHERE relname = '{partition_name.lower()}'
            """)
            
            if cursor.fetchone()[0] == 0:
                try:
                    # Створюємо партицію для цього місяця
                    cursor.execute(f"""
                        CREATE TABLE IF NOT EXISTS {partition_name}
                        PARTITION OF beer_ratings_history
                        FOR VALUES FROM ('{month_date}') TO ('{next_month}');
                    """)
                    print(f"Створено партицію для місяця {month_date.strftime('%Y-%m')}")
                except Exception as e:
                    print(f"Помилка при створенні партиції для місяця {month_date.strftime('%Y-%m')}: {e}")
        
        # Виводимо інформацію про існуючі партиції
        cursor.execute("""
            SELECT c.relname as partition_name, 
                   pg_size_pretty(pg_relation_size(c.oid)) as size
            FROM pg_class c
            JOIN pg_inherits i ON i.inhrelid = c.oid
            JOIN pg_class p ON i.inhparent = p.oid
            WHERE p.relname = 'beer_ratings_history'
            ORDER BY partition_name;
        """)
        
        partitions = cursor.fetchall()
        print("Існуючі партиції:")
        for name, size in partitions:
            print(f"  - {name}: {size}")
    
    return True

# Оператор для створення партицій
create_partitions = PythonOperator(
    task_id='create_time_partitions',
    python_callable=create_time_partitions,
    dag=dag,
)

# Функція для збору та запису історичних даних про рейтинги пива
def collect_beer_ratings_history(**kwargs):
    with get_postgres_connection() as conn:
        cursor = conn.cursor()
        
        # Отримуємо поточну дату як кінець місяця
        current_month = datetime.now().date().replace(day=1)
        
        # Перевіряємо, чи вже є дані за поточний місяць
        cursor.execute("""
            SELECT COUNT(*) FROM beer_ratings_history
            WHERE recorded_month = %s
        """, (current_month,))
        
        existing_records = cursor.fetchone()[0]
        
        if existing_records > 0:
            print(f"Дані за місяць {current_month.strftime('%Y-%m')} вже існують. Пропускаємо.")
            return True
        
        # Збираємо дані про рейтинги з партиційної таблиці стилів
        cursor.execute("""
            INSERT INTO beer_ratings_history (
                beer_id, beer_name, style_id, style_name, 
                brewery_id, brewery_name, average_review_overall, 
                number_of_reviews, recorded_month, recorded_at
            )
            SELECT 
                beer_id, beer_name, style_id, style_name,
                brewery_id, brewery_name, average_review_overall,
                number_of_reviews, %s as recorded_month, 
                CURRENT_TIMESTAMP as recorded_at
            FROM partitioned_beer_data;
        """, (current_month,))
        
        inserted_count = cursor.rowcount
        print(f"Додано {inserted_count} записів з рейтингами пива за місяць {current_month.strftime('%Y-%m')}")
        
        # Створюємо агреговану статистику для нових даних
        cursor.execute("""
            DROP TABLE IF EXISTS temp_monthly_style_stats;
            
            CREATE TEMP TABLE temp_monthly_style_stats AS
            SELECT 
                style_id, 
                style_name,
                AVG(average_review_overall) as avg_style_rating,
                SUM(number_of_reviews) as total_style_reviews,
                COUNT(beer_id) as beer_count,
                recorded_month
            FROM beer_ratings_history
            WHERE recorded_month = %s
            GROUP BY style_id, style_name, recorded_month;
        """, (current_month,))
        
        # Зберігаємо статистику в JSON для подальшого аналізу
        cursor.execute("SELECT * FROM temp_monthly_style_stats")
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        
        stats_df = pd.DataFrame(rows, columns=columns)
        
        # Зберігаємо статистику в файл
        stats_file_path = os.path.join(
            dataset_folder_path, 
            f"style_ratings_{current_month.strftime('%Y_%m')}.json"
        )
        
        stats_df.to_json(stats_file_path, orient='records', date_format='iso', indent=4)
        print(f"Збережено статистику за місяць у файл: {stats_file_path}")
    
    return True

# Функція для аналізу змін у рейтингах пива з часом
def analyze_ratings_changes(**kwargs):
    with get_postgres_connection() as conn:
        cursor = conn.cursor()
        
        # Отримуємо поточну дату як кінець місяця
        current_month = datetime.now().date().replace(day=1)
        previous_month = (current_month - timedelta(days=1)).replace(day=1)
        
        # Створюємо таблицю для аналізу змін у рейтингах
        cursor.execute("""
            DROP TABLE IF EXISTS mart_ratings_change;
            
            CREATE TABLE mart_ratings_change AS
            WITH current_month_data AS (
                SELECT 
                    beer_id, beer_name, style_id, style_name,
                    brewery_id, brewery_name, average_review_overall,
                    number_of_reviews
                FROM beer_ratings_history
                WHERE recorded_month = %s
            ),
            previous_month_data AS (
                SELECT 
                    beer_id, average_review_overall as prev_rating,
                    number_of_reviews as prev_reviews
                FROM beer_ratings_history
                WHERE recorded_month = %s
            )
            SELECT 
                c.beer_id,
                c.beer_name,
                c.style_id,
                c.style_name,
                c.brewery_id,
                c.brewery_name,
                c.average_review_overall as current_rating,
                c.number_of_reviews as current_reviews,
                p.prev_rating,
                p.prev_reviews,
                COALESCE(c.average_review_overall, 0) - COALESCE(p.prev_rating, 0) as rating_change,
                COALESCE(c.number_of_reviews, 0) - COALESCE(p.prev_reviews, 0) as reviews_added,
                %s as analysis_month,
                CURRENT_TIMESTAMP as created_at
            FROM current_month_data c
            LEFT JOIN previous_month_data p ON c.beer_id = p.beer_id
            WHERE 
                p.beer_id IS NOT NULL AND 
                (c.number_of_reviews <> p.prev_reviews OR c.average_review_overall <> p.prev_rating);
        """, (current_month, previous_month, current_month))
        
        change_count = cursor.rowcount
        print(f"Виявлено {change_count} змін у рейтингах пива між {previous_month.strftime('%Y-%m')} та {current_month.strftime('%Y-%m')}")
        
        # Створюємо індекси для оптимізації запитів
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_ratings_change_beer ON mart_ratings_change(beer_id);
            CREATE INDEX IF NOT EXISTS idx_ratings_change_style ON mart_ratings_change(style_id);
            CREATE INDEX IF NOT EXISTS idx_ratings_change_rating ON mart_ratings_change(rating_change);
        """)
        
        # Експортуємо таблицю змін у JSON
        cursor.execute("SELECT * FROM mart_ratings_change")
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        
        changes_df = pd.DataFrame(rows, columns=columns)
        
        # Зберігаємо дані змін у файл
        changes_file_path = os.path.join(
            dataset_folder_path, 
            f"ratings_changes_{current_month.strftime('%Y_%m')}.json"
        )
        
        changes_df.to_json(changes_file_path, orient='records', date_format='iso', indent=4)
        print(f"Збережено аналіз змін рейтингів у файл: {changes_file_path}")
        
        # Виводимо топ-5 пива, яке найбільше покращило рейтинг
        cursor.execute("""
            SELECT 
                beer_name, style_name, brewery_name, 
                current_rating, prev_rating, rating_change, reviews_added
            FROM mart_ratings_change
            WHERE reviews_added >= 5  -- Мінімальна кількість нових відгуків для достовірності
            ORDER BY rating_change DESC
            LIMIT 5;
        """)
        
        top_improved = cursor.fetchall()
        print("Топ-5 пива з найбільшим покращенням рейтингу:")
        for beer in top_improved:
            print(f"  - {beer[0]} ({beer[1]}) від {beer[2]}: {beer[3]:.2f} (було {beer[4]:.2f}, +{beer[5]:.2f}, {beer[6]} нових відгуків)")
    
    return True

# Оператори для збору та аналізу даних
collect_ratings = PythonOperator(
    task_id='collect_beer_ratings_history',
    python_callable=collect_beer_ratings_history,
    dag=dag,
)

analyze_changes = PythonOperator(
    task_id='analyze_ratings_changes',
    python_callable=analyze_ratings_changes,
    dag=dag,
)

def cleanup_old_data(**kwargs):
    with get_postgres_connection() as conn:
        cursor = conn.cursor()
        
        # Визначаємо дату видалення (дані старші за 2 роки)
        retention_limit = (datetime.now().date() - timedelta(days=730)).replace(day=1)
        retention_limit_str = retention_limit.strftime('%Y_%m')
        
        # Отримуємо список застарілих партицій
        cursor.execute("""
            SELECT c.relname as partition_name
            FROM pg_class c
            JOIN pg_inherits i ON i.inhrelid = c.oid
            JOIN pg_class p ON i.inhparent = p.oid
            WHERE p.relname = 'beer_ratings_history'
            AND c.relname < %s
        """, (f'beer_ratings_{retention_limit_str}',))
        
        old_partitions = cursor.fetchall()
        
        for partition in old_partitions:
            partition_name = partition[0]
            try:
                # Видаляємо застарілу партицію
                cursor.execute(f"DROP TABLE IF EXISTS {partition_name}")
                print(f"Видалено застарілу партицію: {partition_name}")
            except Exception as e:
                print(f"Помилка при видаленні партиції {partition_name}: {e}")
        
        print(f"Очищення завершено. Видалено {len(old_partitions)} застарілих партицій")
    
    return True

# Оператор для очищення застарілих даних
cleanup_data = PythonOperator(
    task_id='cleanup_old_data',
    python_callable=cleanup_old_data,
    dag=dag,
)

# Послідовність виконання завдань
create_time_partitioned_beer_ratings >> create_partitions >> collect_ratings >> analyze_changes >> cleanup_data