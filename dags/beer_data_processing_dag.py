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
from airflow.sensors.filesystem import FileSensor
from airflow.models import Variable
from sklearn.preprocessing import MinMaxScaler, RobustScaler

# Константи
dataset_folder_path = json.loads(BaseHook.get_connection('dataset_folder').extra)['path']
beer_data_filename = Variable.get("beer_data_filename", default_var="beer_profile_and_ratings.csv")
CSV_FILE_PATH = os.path.join(dataset_folder_path, beer_data_filename)  # Шлях до CSV файлу
POSTGRES_CONN_ID = 'postgres_conn'  # З'єднання з PostgreSQL

# Налаштування DAG
default_args = {
    'owner': 'airflow',
    'depends_on_past': False,
    'start_date': datetime(2025, 4, 8),
    'email_on_failure': False,
    'email_on_retry': False,
    'retries': 5,
    'retry_delay': timedelta(seconds=30),
}

dag = DAG(
    'beer_data_processing',
    default_args=default_args,
    description='Pipeline для обробки даних про пиво',
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

# Оператор для перевірки існування файлу
check_file_exists = FileSensor(
    task_id='check_file_exists',
    filepath=CSV_FILE_PATH,
    fs_conn_id='dataset_folder',  
    poke_interval=60,  # Перевіряти кожну хвилину
    timeout=60 * 5,  # Таймаут через 5 хвилин
    dag=dag,
)

# Функція для завантаження та базової обробки даних
def load_data(**kwargs):
    # Завантаження даних
    df = pd.read_csv(CSV_FILE_PATH)
    
    # Базова обробка
    df.dropna(subset=['Name', 'Style', 'Brewery'], inplace=True)

    # Групування даних для уникнення дублікатів
    df = df.groupby(['Name', 'Style', 'Brewery']).agg({
        # для числових полів використовуємо sum або mean залежно від типу
        'Astringency': 'sum',
        'Body': 'sum',
        'Alcohol': 'sum',
        'Bitter': 'sum',
        'Sweet': 'sum',
        'Sour': 'sum',
        'Salty': 'sum',
        'Fruits': 'sum',
        'Hoppy': 'sum',
        'Spices': 'sum',
        'Malty': 'sum',
        'review_aroma': 'mean',
        'review_appearance': 'mean',
        'review_palate': 'mean',
        'review_taste': 'mean',
        'review_overall': 'mean',
        'ABV': 'mean',
        'Min IBU': 'mean',
        'Max IBU': 'mean',
        'number_of_reviews': 'sum'  
    }).reset_index()
    
    # Зберігання DataFrame в тимчасовий файл замість XCom
    temp_file_path = os.path.join(dataset_folder_path, 'temp_raw_data.csv')
    df.to_csv(temp_file_path, index=False)
    
    # Зберігаємо лише шлях до файлу через XCom
    kwargs['ti'].xcom_push(key='raw_data_path', value=temp_file_path)
    print(f"Завантажено {len(df)} записів з CSV файлу")
    
    return True

# Modify the process_min_max function to output to its own file
def process_min_max(**kwargs):
    ti = kwargs['ti']
    temp_file_path = ti.xcom_pull(task_ids='load_data', key='raw_data_path')
    df = pd.read_csv(temp_file_path)

    columns_to_normalize = [
        'review_aroma', 'review_appearance', 'review_palate',
        'review_taste', 'review_overall'
    ]
    
    # Filter out columns that don't exist in the dataframe
    existing_columns = [col for col in columns_to_normalize if col in df.columns]
    
    if existing_columns:
        # Save original values for logging
        original_values = df[existing_columns].iloc[0].to_dict()
        
        scaler = MinMaxScaler()
        df[existing_columns] = scaler.fit_transform(df[existing_columns])
        
        # Log transformation for verification
        scaled_values = df[existing_columns].iloc[0].to_dict()
        print(f"MinMax Scaling Example - Original: {original_values}")
        print(f"MinMax Scaling Example - Scaled: {scaled_values}")
    
    # Save to a new file - only save the processed columns plus ID columns
    processed_file_path = os.path.join(dataset_folder_path, 'temp_minmax_data.csv')
    columns_to_save = ['Name', 'Style', 'Brewery'] + existing_columns
    df[columns_to_save].to_csv(processed_file_path, index=False)
    
    # Pass the new file path to the next task
    ti.xcom_push(key='processed_data_path', value=processed_file_path)
    
    return True

# Modify the process_robust_scaler function
def process_robust_scaler(**kwargs): 
    ti = kwargs['ti']
    temp_file_path = ti.xcom_pull(task_ids='load_data', key='raw_data_path')
    df = pd.read_csv(temp_file_path)

    columns_to_normalize = [
        'ABV', 'Min IBU', 'Max IBU', 'number_of_reviews'
    ]
    
    # Filter out columns that don't exist in the dataframe
    existing_columns = [col for col in columns_to_normalize if col in df.columns]
    
    if existing_columns:
        # Save original values for logging
        original_values = df[existing_columns].iloc[0].to_dict()
        
        scaler = RobustScaler()
        df[existing_columns] = scaler.fit_transform(df[existing_columns])
        
        # Log transformation for verification
        scaled_values = df[existing_columns].iloc[0].to_dict()
        print(f"Robust Scaling Example - Original: {original_values}")
        print(f"Robust Scaling Example - Scaled: {scaled_values}")
    
    # Save to a new file - only save the processed columns plus ID columns
    processed_file_path = os.path.join(dataset_folder_path, 'temp_robust_data.csv')
    columns_to_save = ['Name', 'Style', 'Brewery'] + existing_columns
    df[columns_to_save].to_csv(processed_file_path, index=False)
    
    # Pass the new file path to the next task
    ti.xcom_push(key='processed_data_path', value=processed_file_path)
    
    return True

# Modify the process_proportions function
def process_proportions(**kwargs):
    ti = kwargs['ti']
    temp_file_path = ti.xcom_pull(task_ids='load_data', key='raw_data_path')
    df = pd.read_csv(temp_file_path)

    columns_to_normalize = [
        'Astringency', 'Body', 'Alcohol', 'Bitter', 'Sweet', 'Sour',  
        'Salty', 'Fruits', 'Hoppy', 'Spices', 'Malty'
    ]
    
    # Filter out columns that don't exist in the dataframe
    existing_columns = [col for col in columns_to_normalize if col in df.columns]
    
    if existing_columns:
        # Save original values for logging
        original_values = df[existing_columns].iloc[0].to_dict()
        original_sum = sum(original_values.values())
        
        row_sums = df[existing_columns].sum(axis=1)
        
        # Безпечне ділення, щоб уникнути ділення на нуль
        for col in existing_columns:
            df[col] = df[col].div(row_sums).fillna(0)
        
        # Log transformation for verification
        normalized_values = df[existing_columns].iloc[0].to_dict()
        normalized_sum = sum(normalized_values.values())
        print(f"Proportion Normalization Example - Original Sum: {original_sum}")
        print(f"Proportion Normalization Example - Normalized Sum: {normalized_sum}")
        print(f"Original values: {original_values}")
        print(f"Normalized values: {normalized_values}")
    
    # Save to a new file - only save the processed columns plus ID columns
    processed_file_path = os.path.join(dataset_folder_path, 'temp_proportions_data.csv')
    columns_to_save = ['Name', 'Style', 'Brewery'] + existing_columns
    df[columns_to_save].to_csv(processed_file_path, index=False)
    
    # Pass the new file path to the next task
    ti.xcom_push(key='processed_data_path', value=processed_file_path)
    
    return True

def create_merged_data(**kwargs):
    """
    Merge the results from all three normalization tasks into a single dataset.
    Each normalization task modifies different columns, so we need to combine them.
    """
    ti = kwargs['ti']
    
    # Get paths to all processed files
    raw_data_path = ti.xcom_pull(task_ids='load_data', key='raw_data_path')
    
    # Load the original data as a base
    df_base = pd.read_csv(raw_data_path)
    
    # Define which columns are processed by each task
    minmax_columns = ['review_aroma', 'review_appearance', 'review_palate', 'review_taste', 'review_overall']
    robust_columns = ['ABV', 'Min IBU', 'Max IBU', 'number_of_reviews']
    proportion_columns = ['Astringency', 'Body', 'Alcohol', 'Bitter', 'Sweet', 'Sour', 'Salty', 
                          'Fruits', 'Hoppy', 'Spices', 'Malty']
    
    # Try to get results from MinMax scaler
    try:
        # Check if the task has an XCom result
        minmax_path = ti.xcom_pull(task_ids='process_min_max', key='processed_data_path')
        if minmax_path:
            df_minmax = pd.read_csv(minmax_path)
            # Only update columns that exist and were processed
            for col in [c for c in minmax_columns if c in df_minmax.columns]:
                df_base[col] = df_minmax[col]
            print(f"Applied MinMax scaling to {[c for c in minmax_columns if c in df_minmax.columns]}")
    except Exception as e:
        print(f"Warning: Could not apply MinMax scaling: {e}")
    
    # Try to get results from Robust scaler
    try:
        robust_path = ti.xcom_pull(task_ids='process_robust_scaler', key='processed_data_path')
        if robust_path:
            df_robust = pd.read_csv(robust_path)
            # Only update columns that exist and were processed
            for col in [c for c in robust_columns if c in df_robust.columns]:
                df_base[col] = df_robust[col]
            print(f"Applied Robust scaling to {[c for c in robust_columns if c in df_robust.columns]}")
    except Exception as e:
        print(f"Warning: Could not apply Robust scaling: {e}")
    
    # Try to get results from Proportions
    try:
        prop_path = ti.xcom_pull(task_ids='process_proportions', key='processed_data_path')
        if prop_path:
            df_prop = pd.read_csv(prop_path)
            # Only update columns that exist and were processed
            for col in [c for c in proportion_columns if c in df_prop.columns]:
                df_base[col] = df_prop[col]
            print(f"Applied Proportion normalization to {[c for c in proportion_columns if c in df_prop.columns]}")
    except Exception as e:
        print(f"Warning: Could not apply Proportion normalization: {e}")
    
    # Save the merged dataset
    merged_data_path = os.path.join(dataset_folder_path, 'merged_normalized_data.csv')
    df_base.to_csv(merged_data_path, index=False)
    
    # Sample a few rows to verify the merged data
    sample_data = df_base.head(1).T
    print("Sample of merged data (first row, transposed):")
    print(sample_data)
    
    # Pass the path to the merged data for downstream tasks
    ti.xcom_push(key='merged_data_path', value=merged_data_path)
    
    return True

# Update the populate_dim_table function to use the merged data
def populate_dim_table(source_col: str, dim_table: str, dim_name: str, **kwargs):
    # Отримуємо шлях до остаточно обробленого файлу
    ti = kwargs['ti']
    merged_data_path = ti.xcom_pull(task_ids='create_merged_data', key='merged_data_path')
    df = pd.read_csv(merged_data_path)
    
    # Вибір унікальних значень
    dim_df = df[[source_col]].drop_duplicates().reset_index(drop=True)
    dim_df.rename(columns={source_col: f'{dim_name}_name'}, inplace=True)
    
    # Збереження в базу даних
    with get_postgres_connection() as conn:
        cursor = conn.cursor()
        for _, row in dim_df.iterrows():
            cursor.execute(
                f"INSERT INTO {dim_table} ({dim_name}_name) VALUES (%s) ON CONFLICT ({dim_name}_name) DO NOTHING;",
                (row[f'{dim_name}_name'],)
            )
    
        # Завантаження оновлених даних для експорту та маппінгу
        cursor.execute(f"SELECT {dim_name}_id, {dim_name}_name FROM {dim_table} ORDER BY {dim_name}_id")
        dim_rows = cursor.fetchall()
        
    dim_df = pd.DataFrame(dim_rows, columns=[f'{dim_name}_id', f'{dim_name}_name'])
    
    # Створення і збереження маппінгу
    dim_mapping = dict(zip(dim_df[f'{dim_name}_name'], dim_df[f'{dim_name}_id']))
    mapping_path = os.path.join(dataset_folder_path, f'{dim_name}_mapping.json')
    with open(mapping_path, 'w') as f:
        json.dump(dim_mapping, f)
    
    # Повертаємо шлях до маппінгу через XCom замість самих даних
    kwargs['ti'].xcom_push(key=f'{dim_name}_mapping_path', value=mapping_path)
    
    print(f"Створено датасет {dim_table} з {len(dim_df)} записами")
    return True

# Функції-обгортки для виклику populate_dim_table з різними параметрами
def populate_dim_beer(**kwargs):
    return populate_dim_table('Name', 'dim_beer', 'beer', **kwargs)

def populate_dim_style(**kwargs):
    return populate_dim_table('Style', 'dim_style', 'style', **kwargs)

def populate_dim_brewery(**kwargs):
    return populate_dim_table('Brewery', 'dim_brewery', 'brewery', **kwargs)

# Функція для створення факт-таблиці
def populate_fact_table(**kwargs):
    ti = kwargs['ti']
    
    # Завантаження підготовлених даних та маппінгів
    merged_data_path = ti.xcom_pull(task_ids='create_merged_data', key='merged_data_path')
    df = pd.read_csv(merged_data_path)
    
    # Завантаження маппінгів з файлів
    beer_mapping_path = ti.xcom_pull(task_ids='create_dim_beer', key='beer_mapping_path')
    style_mapping_path = ti.xcom_pull(task_ids='create_dim_style', key='style_mapping_path')
    brewery_mapping_path = ti.xcom_pull(task_ids='create_dim_brewery', key='brewery_mapping_path')
    
    with open(beer_mapping_path, 'r') as f:
        beer_mapping = json.load(f)
    with open(style_mapping_path, 'r') as f:
        style_mapping = json.load(f)
    with open(brewery_mapping_path, 'r') as f:
        brewery_mapping = json.load(f)
    
    # Застосування маппінгів
    df['beer_id'] = df['Name'].map(beer_mapping)
    df['style_id'] = df['Style'].map(style_mapping)
    df['brewery_id'] = df['Brewery'].map(brewery_mapping)
    
    # Відбір колонок для факт-таблиці та їх перейменування
    column_mapping = {
        'ABV': 'abv',
        'Min IBU': 'min_ibu',
        'Max IBU': 'max_ibu',
        'Astringency': 'astringency',
        'Body': 'body',
        'Alcohol': 'alcohol',
        'Bitter': 'bitter',
        'Sweet': 'sweet',
        'Sour': 'sour',
        'Salty': 'salty',
        'Fruits': 'fruits',
        'Hoppy': 'hoppy',
        'Spices': 'spices',
        'Malty': 'malty',
        'review_overall': 'average_review_overall'
    }
    
    # Перейменування колонок
    for old_name, new_name in column_mapping.items():
        if old_name in df.columns:
            df.rename(columns={old_name: new_name}, inplace=True)
    
    # Виберемо тільки ті колонки, які нам потрібні
    fact_columns = [
        'beer_id', 'style_id', 'brewery_id', 'abv', 'min_ibu', 'max_ibu',
        'astringency', 'body', 'alcohol', 'bitter', 'sweet', 'sour',
        'salty', 'fruits', 'hoppy', 'spices', 'malty', "review_aroma", 
        "review_appearance", "review_palate", "review_taste", 'average_review_overall',
        'number_of_reviews'
    ]
    
    # Відфільтруємо колонки, які є в даних
    existing_columns = [col for col in fact_columns if col in df.columns]
    fact_df = df[existing_columns]
    
    # Log the first row of fact data for verification
    print("Sample fact data for verification:")
    print(fact_df.iloc[0].to_dict())
    
    # Збереження в базу даних з оптимізованим кодом
    with get_postgres_connection() as conn:
        cursor = conn.cursor()
        
        # Отримуємо поточні записи з fact_beer для порівняння
        try:
            cursor.execute("SELECT beer_id, style_id, brewery_id FROM fact_beer")
            existing_records = set((r[0], r[1], r[2]) for r in cursor.fetchall())
        except Exception as e:
            print(f"Таблиця fact_beer ще не має записів або не існує: {e}")
            existing_records = set()
        
        # Створюємо набір нових записів для порівняння
        new_records = set()
        for _, row in fact_df.iterrows():
            record_key = (row['beer_id'], row['style_id'], row['brewery_id'])
            new_records.add(record_key)
        
        # Знаходимо записи, яких немає в нових даних (видалені з джерела)
        deleted_records = existing_records - new_records
        
        # Оновлюємо поле is_active для видалених записів
        for beer_id, style_id, brewery_id in deleted_records:
            cursor.execute("""
                UPDATE fact_beer 
                SET is_active = FALSE, last_updated = CURRENT_TIMESTAMP 
                WHERE beer_id = %s AND style_id = %s AND brewery_id = %s
            """, (beer_id, style_id, brewery_id))
        
        # Підготовка списку колонок для INSERT
        insert_columns = existing_columns.copy()  # Створюємо копію щоб не змінювати оригінал
        insert_columns.append('is_active')
        insert_columns.append('last_updated')
        columns_str = ", ".join(insert_columns)
        
        # Підготовка значень SQL-запиту
        placeholders = []
        for _ in range(len(existing_columns)):
            placeholders.append("%s")
        # Додаємо константні значення для is_active і last_updated
        placeholders.extend(["TRUE", "CURRENT_TIMESTAMP"])
        placeholders_str = ", ".join(placeholders)
        
        # Підготовка UPDATE частини запиту
        update_clauses = []
        for col in existing_columns:
            if col not in ['beer_id', 'style_id', 'brewery_id']:
                update_clauses.append(f"{col} = EXCLUDED.{col}")
        
        # Додаємо оновлення is_active і last_updated
        update_clauses.append("is_active = TRUE")
        update_clauses.append("last_updated = CURRENT_TIMESTAMP")
        
        update_clause = ",\n".join(update_clauses)
        
        # Підготовка повного запиту
        query = f"""
        INSERT INTO fact_beer ({columns_str})
        VALUES ({placeholders_str})
        ON CONFLICT (beer_id, style_id, brewery_id)
        DO UPDATE SET
        {update_clause};
        """
        
        # Виконання операції вставки для кожного рядка
        for _, row in fact_df.iterrows():
            # Беремо тільки значення тих колонок, які є в даних
            values = [row[col] for col in existing_columns]
            try:
                cursor.execute(query, values)
                print(f"Вставлено/оновлено запис: beer_id={row['beer_id']}, style_id={row['style_id']}, brewery_id={row['brewery_id']}")
            except Exception as e:
                print(f"Помилка при вставці запису: {e}")
                print(f"Дані запису: {values}")
                conn.rollback()  # Відкочуємо транзакцію, щоб продовжити з іншими записами
                continue
        
        try:
            # Отримання оновлених даних для звіту
            cursor.execute("SELECT * FROM fact_beer")
            fact_rows = cursor.fetchall()
            colnames = [desc[0] for desc in cursor.description]
            
            fact_df = pd.DataFrame(fact_rows, columns=colnames)
            
            print(f"Створено факт-таблицю з {len(fact_df)} записами")
            print(f"З них активних: {fact_df['is_active'].sum()}")
            print(f"Неактивних (видалених з джерела): {len(fact_df) - fact_df['is_active'].sum()}")

            json_file_path = os.path.join(dataset_folder_path, "fact_table.json")
            fact_df.to_json(json_file_path, orient="records", force_ascii=False, indent=4)

            print(f"Saved fact table data to JSON at: {json_file_path}")
        except Exception as e:
            print(f"Помилка при отриманні статистики: {e}")
    
    return True

# Створення таблиць в базі даних
create_dim_beer = SQLExecuteQueryOperator(
    task_id='create_dim_beer_table',
    conn_id=POSTGRES_CONN_ID,
    sql="""
    CREATE TABLE IF NOT EXISTS dim_beer (
        beer_id SERIAL PRIMARY KEY,
        beer_name VARCHAR(255) NOT NULL UNIQUE
    );
    """,
    dag=dag,
)

create_dim_style = SQLExecuteQueryOperator(
    task_id='create_dim_style_table',
    conn_id=POSTGRES_CONN_ID,
    sql="""
    CREATE TABLE IF NOT EXISTS dim_style (
        style_id SERIAL PRIMARY KEY,
        style_name VARCHAR(255) NOT NULL UNIQUE
    );
    """,
    dag=dag,
)

create_dim_brewery = SQLExecuteQueryOperator(
    task_id='create_dim_brewery_table',
    conn_id=POSTGRES_CONN_ID,
    sql="""
    CREATE TABLE IF NOT EXISTS dim_brewery (
        brewery_id SERIAL PRIMARY KEY,
        brewery_name VARCHAR(255) NOT NULL UNIQUE
    );
    """,
    dag=dag,
)

create_fact_beer = SQLExecuteQueryOperator(
    task_id='create_fact_beer_table',
    conn_id=POSTGRES_CONN_ID,
    sql="""
    CREATE TABLE IF NOT EXISTS fact_beer (
        beer_id INTEGER REFERENCES dim_beer(beer_id),
        style_id INTEGER REFERENCES dim_style(style_id),
        brewery_id INTEGER REFERENCES dim_brewery(brewery_id),
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
        number_of_reviews INTEGER DEFAULT 1,
        is_active BOOLEAN DEFAULT TRUE,
        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (beer_id, style_id, brewery_id)
    );
    """,
    dag=dag,
)

# Оператори для кожного кроку ETL
load_data = PythonOperator(
    task_id='load_data',
    python_callable=load_data,
    dag=dag,
)

create_dim_beer_task = PythonOperator(
    task_id='create_dim_beer',
    python_callable=populate_dim_beer,
    dag=dag,
)

create_dim_style_task = PythonOperator(
    task_id='create_dim_style',
    python_callable=populate_dim_style,
    dag=dag,
)

create_dim_brewery_task = PythonOperator(
    task_id='create_dim_brewery',
    python_callable=populate_dim_brewery,
    dag=dag,
)

create_fact_table_task = PythonOperator(
    task_id='create_fact_table',
    python_callable=populate_fact_table,
    dag=dag,
)

# Оператори для нормалізації даних
process_min_max_task = PythonOperator(
    task_id='process_min_max',
    python_callable=process_min_max,
    dag=dag,
)

process_robust_scaler_task = PythonOperator(
    task_id='process_robust_scaler',
    python_callable=process_robust_scaler,
    dag=dag,
)

process_proportions_task = PythonOperator(
    task_id='process_proportions',
    python_callable=process_proportions,
    dag=dag,
)

# Create the new merge task operator
create_merged_data_task = PythonOperator(
    task_id='create_merged_data',
    python_callable=create_merged_data,
    dag=dag,
)

# Updated dependencies
# Load data first
check_file_exists >> load_data

# Run normalizations in parallel since they affect different columns
load_data >> [process_min_max_task, process_robust_scaler_task, process_proportions_task]

# Wait for ALL normalizations to complete before merging data
[process_min_max_task, process_robust_scaler_task, process_proportions_task] >> create_merged_data_task

# Create tables after merged data is ready
create_merged_data_task >> [create_dim_beer, create_dim_style, create_dim_brewery, create_fact_beer]

# Populate dimension tables
create_dim_beer >> create_dim_beer_task
create_dim_style >> create_dim_style_task
create_dim_brewery >> create_dim_brewery_task

# Create fact table after all dimensions
[create_fact_beer, create_dim_beer_task, create_dim_style_task, create_dim_brewery_task] >> create_fact_table_task