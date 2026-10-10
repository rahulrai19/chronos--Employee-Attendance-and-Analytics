# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
5. **One thing you would change** if this had to serve 100x the data.


# Decisions description by me 

# 1.Indexes
I created a unique index on employees(emp_code) to prevent duplicate registrations,and a compound unique index on attendance_logs(emp_code,date) to strict guarantee one log per employee per day. I also created date DESC emp_code ASC to paginate the attendence and employee for analytics filtering. I considered but rejected an index on status alone because it is too low cardinality to be useful for query performance

# 2.Punch-in race
if two identical requests arrive simultaneously they both build the document and attempt to call db.attendance.insert_one. because the employee_code and date are indexed MongoDB handles the atomicity lock at storage level and returns duplicate key error on the second request, the first request succeeds and also return the 409 conflict response.

# 3.Ties
My leaderboard uses MongoDB`s $rank window function to provide standard competition ranking meaning tied employees get the exact same rank number.To guarantee deterministic results for the same data i have added secondary sorting by emp_code. This ensures that the ranking is consistent even if employees have the same late minutes.

# 4.Headcount
To ensure employees with zero logs are counted the department summary aggregation begin by querying the employees collection filtering by department and joined_on rahter than starting from the logs collection. It then performs a $lookup to fetch their logs This ensures every valid employee exist in the pipeline to be counted as headcount even if their &lookup array comes back completely empty.

# 5.Thing I would change
if i had to scale to 100x the data,I would move away from running heavy real time aggregations Instead I would implement views or MongoDB Triggers to pre calculate and aggregate metrics into a daily or hourly into sperate collection whenever the punch occurs.