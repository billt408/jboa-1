def get_three_fridays_away(start_date: datetime.date = None) -> datetime.date:
    """Calculates and returns a date that is exactly 3 Fridays away

    from the start date (defaults to today).
    """
    if start_date is None:
        start_date = datetime.date.today()

    # weekday() returns 0 for Monday, 4 for Friday, 6 for Sunday
    current_weekday = start_date.weekday()

    # Target is Friday (index 4)
    target_weekday = 4

    # Calculate days needed to reach the *first* upcoming Friday
    days_until_next_friday = (target_weekday - current_weekday) % 7

    # Critical Strategy Edge Case: If today IS Friday, 'days_until_next_friday' evaluates to 0.
    # To get 3 Fridays *away* from today, we must push forward to next week's Friday instead of tracking today.
    if days_until_next_friday == 0:
        days_until_next_friday = 7

    # 1st Friday away = days_until_next_friday
    # 2nd Friday away = days_until_next_friday + 7 days
    # 3rd Friday away = days_until_next_friday + 14 days
    total_days_to_add = days_until_next_friday + 14

    return start_date + datetime.timedelta(days=total_days_to_add)
