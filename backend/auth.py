from werkzeug.security import check_password_hash, generate_password_hash
from backend.db import get_db_connection

def authenticate_user(username, password):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id, username, password_hash, full_name, role FROM accounts_users WHERE username = %s;", (username,))
        user = cursor.fetchone()
        if user and check_password_hash(user['password_hash'], password):
            return {
                'id': user['id'],
                'username': user['username'],
                'full_name': user['full_name'],
                'role': user['role']
            }
        return None
    finally:
        cursor.close()
        conn.close()

def change_user_password(username, old_password, new_password):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id, password_hash FROM accounts_users WHERE username = %s;", (username,))
        user = cursor.fetchone()
        if not user or not check_password_hash(user['password_hash'], old_password):
            return False, "Current password is incorrect."
        
        new_hash = generate_password_hash(new_password)
        cursor.execute("UPDATE accounts_users SET password_hash = %s WHERE username = %s;", (new_hash, username))
        conn.commit()
        return True, "Password updated successfully."
    finally:
        cursor.close()
        conn.close()
