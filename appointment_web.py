
import streamlit as st
import pandas as pd
from datetime import datetime, timedelta, date
from io import BytesIO

# ------------------------
# Scheduling Functions
# ------------------------

def preprocess(df):
    df = df[df['Status'] == 'Complete'].copy()
    df['Start'] = pd.to_datetime(df['Start'])
    df['End'] = pd.to_datetime(df['End'])
    df['Created At'] = pd.to_datetime(df['Created At']).dt.date
    df['Original_Date'] = df['Start'].dt.date
    df['Duration'] = (df['End'] - df['Start']).dt.total_seconds() / 60
    df = df.reset_index(drop=True)
    df['Appt_ID'] = df.index
    return df[['Appt_ID', 'Medication', 'Duration', 'Original_Date', 'Created At']].dropna()

def get_clinic_days_same_month(date_obj):
    first_day = date_obj.replace(day=1)
    next_month = (first_day + timedelta(days=32)).replace(day=1)
    last_day = next_month - timedelta(days=1)
    clinic_days = []
    current = first_day
    while current <= last_day:
        if current.weekday() in [1, 2, 3]:  # Tue, Wed, Thu
            clinic_days.append(current)
        current += timedelta(days=1)
    return clinic_days

def allocate_appointments(df):
    CLINIC_MINUTES = 540
    allocations = []
    unassigned = []
    for medication, group in df.groupby('Medication'):
        appts = group.sort_values(by=['Original_Date', 'Duration'], ascending=[True, False]).to_dict('records')
        schedule = {}
        for appt in appts:
            original_date = appt['Original_Date']
            duration = appt['Duration']
            same_month_days = get_clinic_days_same_month(original_date)
            same_month_days.sort(key=lambda d: abs((d - original_date).days))
            assigned = False
            for day in same_month_days:
                if day not in schedule:
                    schedule[day] = {'total': 0, 'items': []}
                if schedule[day]['total'] + duration <= CLINIC_MINUTES:
                    schedule[day]['items'].append(appt)
                    schedule[day]['total'] += duration
                    assigned = True
                    break
            if not assigned:
                unassigned.append(appt)
        for day, data in schedule.items():
            for item in data['items']:
                item['Assigned_Date'] = day
                allocations.append(item)
    return pd.DataFrame(allocations), pd.DataFrame(unassigned)

def rebalance_schedule_strict_global(assigned_df):
    CLINIC_MINUTES = 540
    assigned_df = assigned_df.sort_values(by=['Medication', 'Assigned_Date', 'Duration'], ascending=[True, True, False])
    rebalanced_rows = []
    day_totals = {}
    for _, row in assigned_df.iterrows():
        day = row['Assigned_Date']
        day_totals[day] = day_totals.get(day, 0) + row['Duration']
    months = assigned_df['Assigned_Date'].apply(lambda d: (d.year, d.month)).unique()
    all_clinic_days = set()
    for (year, month) in months:
        first_day = date(year, month, 1)
        if month == 12:
            next_month = date(year+1, 1, 1)
        else:
            next_month = date(year, month+1, 1)
        last_day = next_month - timedelta(days=1)
        current = first_day
        while current <= last_day:
            if current.weekday() in [1, 2, 3]:
                all_clinic_days.add(current)
            current += timedelta(days=1)
    for medication, group in assigned_df.groupby('Medication'):
        schedule = {}
        for _, row in group.iterrows():
            day = row['Assigned_Date']
            schedule.setdefault(day, []).append(row.to_dict())
        days_sorted = sorted(all_clinic_days)
        max_iterations = 1000
        iteration = 0
        changed = True
        while changed and iteration < max_iterations:
            iteration += 1
            changed = False
            for i, day in enumerate(days_sorted):
                total = sum(appt['Duration'] for appt in schedule.get(day, []))
                while total > CLINIC_MINUTES or day_totals.get(day, 0) > CLINIC_MINUTES:
                    appts_today = schedule.get(day, [])
                    if not appts_today:
                        break
                    appts_today.sort(key=lambda x: x['Duration'], reverse=True)
                    moved = False
                    for appt in appts_today:
                        for prev_day in reversed(days_sorted[:i]):
                            if prev_day < appt['Created At']:
                                continue
                            if day_totals.get(prev_day, 0) + appt['Duration'] <= CLINIC_MINUTES:
                                schedule.setdefault(prev_day, []).append(appt)
                                schedule[day].remove(appt)
                                day_totals[prev_day] = day_totals.get(prev_day, 0) + appt['Duration']
                                day_totals[day] -= appt['Duration']
                                total -= appt['Duration']
                                changed = True
                                moved = True
                                break
                        if moved:
                            break
                        for next_day in days_sorted[i+1:]:
                            if day_totals.get(next_day, 0) + appt['Duration'] <= CLINIC_MINUTES:
                                schedule.setdefault(next_day, []).append(appt)
                                schedule[day].remove(appt)
                                day_totals[next_day] = day_totals.get(next_day, 0) + appt['Duration']
                                day_totals[day] -= appt['Duration']
                                total -= appt['Duration']
                                changed = True
                                moved = True
                                break
                        if moved:
                            break
                    if not moved:
                        break
                for later_day in days_sorted[i+1:]:
                    later_appts = schedule.get(later_day, [])[:]
                    for appt in later_appts:
                        if day < appt['Created At']:
                            continue
                        if day_totals.get(day, 0) + appt['Duration'] <= CLINIC_MINUTES:
                            schedule.setdefault(day, []).append(appt)
                            schedule[later_day].remove(appt)
                            day_totals[day] += appt['Duration']
                            day_totals[later_day] -= appt['Duration']
                            changed = True
        for day in days_sorted:
            for appt in schedule.get(day, []):
                appt['Assigned_Date'] = day
                appt['Days_Moved'] = (day - appt['Original_Date']).days
                rebalanced_rows.append(appt)
    return pd.DataFrame(rebalanced_rows), day_totals

def suggest_appointment_slots(day_totals, proposed_date, duration, created_at=None):
    CLINIC_MINUTES = 540
    created_at = created_at or date.today()
    month_start = proposed_date.replace(day=1)
    if proposed_date.month == 12:
        next_month = proposed_date.replace(year=proposed_date.year+1, month=1, day=1)
    else:
        next_month = proposed_date.replace(month=proposed_date.month+1, day=1)
    month_end = next_month - timedelta(days=1)
    clinic_days = []
    current = month_start
    while current <= month_end:
        if current.weekday() in [1, 2, 3] and current >= created_at:
            clinic_days.append(current)
        current += timedelta(days=1)
    available_days = []
    for day in clinic_days:
        total_used = day_totals.get(day, 0)
        remaining = CLINIC_MINUTES - total_used
        if remaining >= duration:
            score = abs((day - proposed_date).days)
            available_days.append((day, remaining, score))
    available_days.sort(key=lambda x: (x[2], -x[1]))
    return available_days[:3]

# ------------------------
# Streamlit Web UI
# ------------------------

st.set_page_config(page_title="Appointment Scheduler", layout="centered")
st.title("💊 Appointment Scheduler")

uploaded_file = st.file_uploader("Upload Appointments_Vegas Excel file", type=["xlsx"])

if uploaded_file:
    try:
        df_raw = pd.read_excel(uploaded_file)
        df_clean = preprocess(df_raw)
        assigned_df, _ = allocate_appointments(df_clean)
        if 'Assigned_Date' not in assigned_df.columns:
            assigned_df['Assigned_Date'] = assigned_df['Original_Date']
        rebalanced_df, day_totals = rebalance_schedule_strict_global(assigned_df)

        st.success("File uploaded and schedule optimized successfully!")

        with st.form("appointment_form"):
            st.subheader("Suggest a New Appointment")

            proposed_date = st.date_input("Desired Date", value=date.today())
            duration = st.number_input("Duration (minutes)", min_value=1, max_value=540, value=45)
            created_at = st.date_input("Created At (optional)", value=date.today())

            submitted = st.form_submit_button("Suggest Slots")

        if submitted:
            st.markdown("### 📅 Suggested Appointment Dates")
            created = created_at if created_at else None
            suggestions = suggest_appointment_slots(
                day_totals=day_totals,
                proposed_date=proposed_date,
                duration=duration,
                created_at=created
            )
            if suggestions:
                for d, remaining, _ in suggestions:
                    st.success(f"{d.strftime('%A, %Y-%m-%d')} — {remaining} minutes remaining")
            else:
                st.error("No available slots under current constraints.")

    except Exception as e:
        st.error(f"Error processing file: {e}")
else:
    st.info("Please upload an Excel file with completed appointment data.")
