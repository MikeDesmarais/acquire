#!/usr/bin/env python3.4m

import collections
import glob
import orm
import os
import os.path
import sqlalchemy.orm
import sqlalchemy.sql
import sqlalchemy.types
import subprocess
import time
import trueskill
import ujson


class Logs2DB:
    rating_type_to_draw_probability = {
        'Singles2': .0042,
        'Singles3': .0022,
        'Singles4': .0161,
        'Teams': .0023,
    }

    def __init__(self, session, lookup):
        self.session = session
        self.lookup = lookup
        self.trueskill_environment_lookup = {}
        self.completed_game_users = None

        self.method_lookup = {
            'game': self.process_game,
            'game-import': self.process_game_import,
            'game-player': self.process_game_player,
            'game-result': self.process_game_result,
        }

    def process_logs(self, file, log_time=None):
        len_last_line = 0
        self.completed_game_users = set()
        for line in file:
            if line and line[-1] == '\n':
                if line[0] == '{':
                    params = ujson.decode(line)
                    params['_log-time'] = log_time
                    self.method_lookup[params['_']](params)
            else:
                len_last_line = len(line.encode())
        return file.tell() - len_last_line, self.completed_game_users

    def process_game(self, params):
        game = self.lookup.get_game(params['_log-time'], params['game-id'])

        begin_time = params.get('begin', None)
        if begin_time:
            game.begin_time = begin_time

        end_time = params.get('end', None)
        if end_time:
            game.end_time = end_time

        game.game_state = self.lookup.get_game_state(params['state'])

        game_mode = params.get('mode', None)
        if game_mode:
            game.game_mode = self.lookup.get_game_mode(game_mode)

        score = params.get('score', None)
        if score:
            params['scores'] = score
            self.process_game_result(params)

        game.imported = 0

    def process_game_import(self, params):
        game = orm.Game()
        self.session.add(game)
        game.log_time = params['end'] - 1000000000
        game.number = len(params['scores']) - 1 if params['mode'] == 'Singles' else 4
        game.begin_time = params['end'] - 300
        game.end_time = params['end']
        game.game_state = self.lookup.get_game_state('Completed')
        game.game_mode = self.lookup.get_game_mode(params['mode'])
        game.imported = 1

        game_players = []
        for player_index, name_and_score in enumerate(params['scores']):
            name, score = name_and_score
            game_player = orm.GamePlayer()
            self.session.add(game_player)
            game_player.game = game
            game_player.player_index = player_index
            game_player.user = self.lookup.get_user(name)
            game_player.score = score
            game_players.append(game_player)
            self.completed_game_users.add(game_player.user)

        self.calculate_new_ratings(game, game_players)

    def process_game_player(self, params):
        game = self.lookup.get_game(params['_log-time'], params['game-id'])
        game_player = self.lookup.get_game_player(game, params['player-id'])
        game_player.user = self.lookup.get_user(params['username'])

    def process_game_result(self, params):
        game = self.lookup.get_game(params['_log-time'], params['game-id'])

        game_players = []
        num_players = len(params['scores'])
        for player_index, score in enumerate(params['scores']):
            game_player = self.lookup.get_game_player(game, player_index)
            game_player.score = score
            game_players.append(game_player)
            if num_players >= 2:
                self.completed_game_users.add(game_player.user)

        self.calculate_new_ratings(game, game_players)

    def calculate_new_ratings(self, game, game_players):
        game_mode_name = game.game_mode.name
        num_players = len(game_players)
        if game_mode_name == 'Teams':
            rating_type = self.lookup.get_rating_type('Teams')
        elif game_mode_name == 'Singles' and 2 <= num_players <= 4:
            rating_type = self.lookup.get_rating_type('Singles' + str(num_players))
        else:
            return

        trueskill_ratings = []
        for game_player in game_players:
            rating = self.lookup.get_rating(game_player.user, rating_type)
            if not rating:
                rating = orm.Rating(user=game_player.user, rating_type=rating_type, time=game.begin_time, mu=trueskill.MU, sigma=trueskill.SIGMA)
                self.session.add(rating)
            trueskill_rating = trueskill.Rating(rating.mu, rating.sigma)
            trueskill_ratings.append(trueskill_rating)

        new_ratings = [orm.Rating(user=game_player.user, rating_type=rating_type, time=game.end_time) for game_player in game_players]
        self.session.add_all(new_ratings)

        trueskill_environment = self.get_trueskill_environment(rating_type)

        if game_mode_name == 'Teams':
            rating_groups = [[trueskill_ratings[0], trueskill_ratings[2]], [trueskill_ratings[1], trueskill_ratings[3]]]
            ranks = [-(game_players[0].score + game_players[2].score), -(game_players[1].score + game_players[3].score)]
            rating_groups_result = trueskill_environment.rate(rating_groups, ranks)
            new_ratings[0].mu = rating_groups_result[0][0].mu
            new_ratings[0].sigma = rating_groups_result[0][0].sigma
            new_ratings[1].mu = rating_groups_result[1][0].mu
            new_ratings[1].sigma = rating_groups_result[1][0].sigma
            new_ratings[2].mu = rating_groups_result[0][1].mu
            new_ratings[2].sigma = rating_groups_result[0][1].sigma
            new_ratings[3].mu = rating_groups_result[1][1].mu
            new_ratings[3].sigma = rating_groups_result[1][1].sigma
        else:
            rating_groups = [[trueskill_rating] for trueskill_rating in trueskill_ratings]
            ranks = [[-game_player.score] for game_player in game_players]
            rating_groups_result = trueskill_environment.rate(rating_groups, ranks)
            for player_index, rating_group_result in enumerate(rating_groups_result):
                new_ratings[player_index].mu = rating_group_result[0].mu
                new_ratings[player_index].sigma = rating_group_result[0].sigma

        for rating in new_ratings:
            self.lookup.add_rating(rating)

    def get_trueskill_environment(self, rating_type):
        trueskill_environment = self.trueskill_environment_lookup.get(rating_type.name)
        if trueskill_environment:
            return trueskill_environment

        trueskill_environment = trueskill.TrueSkill(beta=trueskill.SIGMA, draw_probability=Logs2DB.rating_type_to_draw_probability[rating_type.name])

        self.trueskill_environment_lookup[rating_type.name] = trueskill_environment
        return trueskill_environment


class StatsGen:
    users_sql = sqlalchemy.sql.text('''
        select distinct user.user_id,
            user.name
        from user
        join rating on user.user_id = rating.user_id
    ''')
    ratings_sql = sqlalchemy.sql.text('''
        select rating.user_id,
            rating_type.name as rating_type,
            rating.time,
            rating.mu,
            rating.sigma,
            rating_summary.num_games
        from rating
        join (
            select max(rating_id) as rating_id,
                count(rating_id) - 1 as num_games
            from rating
            group by user_id, rating_type_id
        ) rating_summary on rating.rating_id = rating_summary.rating_id
        join rating_type on rating.rating_type_id = rating_type.rating_type_id
        where rating.time >= unix_timestamp() - 30 * 24 * 60 * 60
        order by rating.mu - rating.sigma * 3 desc, rating.mu desc, rating.time asc, rating.user_id asc
    ''')
    user_ratings_sql = sqlalchemy.sql.text('''
        select rating_type.name,
            rating.time,
            rating.mu,
            rating.sigma
        from rating
        join rating_type on rating.rating_type_id = rating_type.rating_type_id
        where rating.user_id = :user_id
        order by rating.time asc, rating.rating_id asc
    ''')
    user_games_sql = sqlalchemy.sql.text('''
        select game.game_id,
            game.end_time,
            game.game_mode_id,
            game_player.user_id,
            game_player.score
        from game
        join (
            select game.game_id
            from game
            join game_player on game.game_id = game_player.game_id
            where game_player.user_id = :user_id
                and game_player.score is not null
        ) game_ids on game.game_id = game_ids.game_id
        join game_player on game.game_id = game_player.game_id
        order by game.end_time desc, game.game_id desc, game_player.player_index asc
    ''')

    def __init__(self, session, output_dir):
        self.session = session
        self.output_dir = output_dir

    def get_user_id_to_name(self):
        user_id_to_name = {}
        for row in self.session.execute(StatsGen.users_sql):
            user_id_to_name[row.user_id] = row.name.decode()
        return user_id_to_name

    def output_users(self, user_id_to_name):
        rating_type_to_ratings = collections.defaultdict(list)
        for row in self.session.execute(StatsGen.ratings_sql):
            rating_type_to_ratings[row.rating_type.decode()].append([row.user_id, row.time, row.mu, row.sigma, row.num_games])

        self.write_file('users', {'users': user_id_to_name, 'ratings': rating_type_to_ratings})

    def output_user(self, user_id):
        ratings = collections.defaultdict(list)
        for row in self.session.execute(StatsGen.user_ratings_sql, {'user_id': user_id}):
            ratings[row.name].append([row.time, row.mu, row.sigma])

        games = []
        last_game_id = None
        for row in self.session.execute(StatsGen.user_games_sql, {'user_id': user_id}):
            if row.game_id != last_game_id:
                games.append([row.game_mode_id, row.end_time, []])
            games[-1][2].append([row.user_id, row.score])
            last_game_id = row.game_id
        games = [game for game in games if len(game[2]) > 1]

        self.write_file('user' + str(user_id), {'ratings': ratings, 'games': games})

    def write_file(self, filename_prefix, contents):
        with open(os.path.join(self.output_dir, filename_prefix + '.json'), 'w') as f:
            f.write(ujson.dumps(contents))


def main():
    user_id_to_name = None

    while True:
        with orm.session_scope() as session:
            lookup = orm.Lookup(session)
            logs2db = Logs2DB(session, lookup)

            kv_last_filename = lookup.get_key_value('cron last filename')
            last_filename = 0 if kv_last_filename.value is None else int(kv_last_filename.value)
            kv_last_offset = lookup.get_key_value('cron last offset')
            last_offset = 0 if kv_last_offset.value is None else int(kv_last_offset.value)

            filenames = []
            for filename in os.listdir('logs_py'):
                filename = int(filename)
                if filename >= last_filename:
                    filenames.append(filename)
            filenames.sort()

            filename = 0
            offset = 0
            completed_game_users = set()
            for filename in filenames:
                offset = last_offset if filename == last_filename else 0
                with open('logs_py/' + str(filename), 'r') as f:
                    if offset:
                        f.seek(offset)
                    offset, new_completed_game_users = logs2db.process_logs(f, filename)
                    completed_game_users.update(new_completed_game_users)

            kv_last_filename.value = filename
            kv_last_offset.value = offset

            session.flush()

            if completed_game_users:
                statsgen = StatsGen(session, 'stats_temp')
                if not user_id_to_name:
                    user_id_to_name = statsgen.get_user_id_to_name()
                for user in completed_game_users:
                    user_id_to_name[user.user_id] = user.name
                statsgen.output_users(user_id_to_name)
                for user in completed_game_users:
                    statsgen.output_user(user.user_id)

                filenames = glob.glob('stats_temp/*.json')
                if filenames:
                    all_filenames = filenames + [x + '.gz' for x in filenames]

                    command = ['zopfli']
                    command.extend(filenames)
                    subprocess.call(command)

                    command = ['touch', '-r', 'stats_temp/users.json']
                    command.extend(all_filenames)
                    subprocess.call(command)

                    command = ['mv']
                    command.extend(all_filenames)
                    command.append('web/stats')
                    subprocess.call(command)

        time.sleep(60)


def recalculate_ratings():
    with orm.session_scope() as session:
        lookup = orm.Lookup(session)
        logs2db = Logs2DB(session, lookup)

        query = sqlalchemy.sql.text('''
            select game.log_time,
                game.number,
                count(distinct game_player.game_player_id) as num_players
            from game
            join game_state on game.game_state_id = game_state.game_state_id
            join game_player on game.game_id = game_player.game_id
            where game_state.name = 'Completed'
            group by game.game_id
            having num_players > 1
            order by game.end_time asc
        ''')
        for row in session.execute(query):
            game = lookup.get_game(row.log_time, row.number)
            game_players = []
            for player_index in range(row.num_players):
                game_players.append(lookup.get_game_player(game, player_index))
            logs2db.calculate_new_ratings(game, game_players)


if __name__ == '__main__':
    main()
    # recalculate_ratings()
