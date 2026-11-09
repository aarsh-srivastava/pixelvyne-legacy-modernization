-- Same rows as samples/oracle/02_seed_data.sql.

INSERT INTO departments VALUES (10, 'Leadership');
INSERT INTO departments VALUES (20, 'Engineering');
INSERT INTO departments VALUES (30, 'Operations');
INSERT INTO departments VALUES (40, 'Finance');

INSERT INTO employees VALUES (101, 'Asha Verma',   'asha@pv.test',   10, NULL, DATE '2015-01-12', 240000,    'A',  'ACTIVE');
INSERT INTO employees VALUES (102, 'Rohan Mehta',  'rohan@pv.test',  20, 101,  DATE '2018-06-01', 120000,    'A',  'ACTIVE');
INSERT INTO employees VALUES (103, 'Priya Nair',   'priya@pv.test',  30, 101,  DATE '2019-03-15', 98000,     'B',  'ACTIVE');
INSERT INTO employees VALUES (104, 'Karan Singh',  'karan@pv.test',  20, 102,  DATE '2021-07-19', 85000.50,  'B',  'ACTIVE');
INSERT INTO employees VALUES (105, 'Neha Gupta',   'neha@pv.test',   20, 102,  DATE '2022-11-30', 85000.50,  'C',  'ACTIVE');
INSERT INTO employees VALUES (106, 'Vikram Rao',   'vikram@pv.test', 30, 103,  DATE '2023-01-31', 64000,     NULL, 'ACTIVE');
INSERT INTO employees VALUES (107, 'Sana Khan',    'sana@pv.test',   30, 103,  DATE '2024-02-29', 72000,     'A',  'ACTIVE');
INSERT INTO employees VALUES (108, 'Arjun Das',    'arjun@pv.test',  20, 104,  DATE '2026-03-31', 56000,     NULL, 'ACTIVE');
INSERT INTO employees VALUES (109, 'Meera Iyer',   NULL,             20, 104,  DATE '2026-09-17', 50000,     NULL, 'ACTIVE');
INSERT INTO employees VALUES (110, 'Dev Malhotra', 'dev@pv.test',    10, 101,  DATE '2017-08-08', 150000,    'B',  'INACTIVE');
INSERT INTO employees VALUES (111, 'Ira Kapoor',   'ira@pv.test',    30, 103,  DATE '2020-10-05', 101000.75, 'C',  'ACTIVE');
