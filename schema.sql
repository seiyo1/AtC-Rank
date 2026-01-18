create table if not exists users (
  discord_id integer primary key,
  atcoder_id text not null,
  is_active integer not null default 1,
  registered_at text not null default CURRENT_TIMESTAMP
);

create unique index if not exists users_atcoder_id_idx on users(atcoder_id);

create table if not exists settings (
  guild_id integer primary key,
  notify_channel_id integer,
  rank_channel_id integer,
  rank_message_id integer,
  role_weekly_id integer,
  role_streak_id integer,
  health_channel_id integer,
  ai_enabled integer not null default 1,
  ai_probability integer not null default 20,
  poll_interval_seconds integer not null default 180
);

create table if not exists problems (
  problem_id text primary key,
  contest_id text,
  title text,
  difficulty_raw real,
  difficulty integer
);

create table if not exists ratings (
  discord_id integer primary key references users(discord_id),
  rating integer,
  updated_at text
);

create table if not exists user_fetch_state (
  discord_id integer primary key references users(discord_id),
  last_checked_epoch integer not null default 1768748400,
  last_submission_id integer
);

create table if not exists user_problem_last_ac (
  discord_id integer references users(discord_id),
  problem_id text references problems(problem_id),
  last_ac_at text not null,
  primary key (discord_id, problem_id)
);

create table if not exists streaks (
  discord_id integer primary key references users(discord_id),
  current_streak integer not null default 0,
  last_ac_date text
);

create table if not exists weekly_scores (
  week_start text not null,
  discord_id integer references users(discord_id),
  score integer not null default 0,
  score_updated_at text not null default CURRENT_TIMESTAMP,
  primary key (week_start, discord_id)
);

create table if not exists weekly_reports (
  week_start text primary key,
  reset_time text not null,
  report_text text,
  ai_comment text
);

create table if not exists submissions (
  id integer primary key autoincrement,
  discord_id integer references users(discord_id),
  problem_id text references problems(problem_id),
  submitted_at text not null,
  score_base integer not null,
  streak_mult real not null,
  score_final integer not null
);

create table if not exists role_colors (
  guild_id integer not null,
  color_key text not null,
  role_id integer not null,
  primary key (guild_id, color_key)
);

create index if not exists submissions_discord_time_idx on submissions(discord_id, submitted_at);
create index if not exists weekly_scores_rank_idx on weekly_scores(week_start, score desc, score_updated_at asc);
