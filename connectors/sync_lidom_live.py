#!/usr/bin/env python3
"""Produce un JSON estático de LIDOM para Sur Wake Up (v3.180).
Ejecutar periódicamente en un servidor o GitHub Actions; no requiere llave API.
No declarar sincronización correcta si el proveedor falla o devuelve un formato inesperado.
"""
import json
import os
import sys
import tempfile
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import requests

BASE='https://statsapi.mlb.com/api/v1'
DST=Path(__file__).resolve().parents[1]/'data'/'source_cache'/'lidom_live.json'
ZONE=ZoneInfo('America/Santo_Domingo')
# Nombres cortos y largos observados en las respuestas de la fuente.
TEAM_ALIASES={
 'tigres del licey':'LIC','tigres':'LIC','licey':'LIC','lice':'LIC',
 'leones del escogido':'ESC','leones':'ESC','escogido':'ESC',
 'aguilas cibaeñas':'AGU','aguilas cibaenas':'AGU','aguilas':'AGU',
 'estrellas orientales':'EST','estrellas':'EST',
 'toros del este':'TOR','toros':'TOR',
 'gigantes del cibao':'GIG','gigantes':'GIG',
}
ALL_TEAMS=('LIC','ESC','AGU','EST','TOR','GIG')

def normalized_name(value):
 value=unicodedata.normalize('NFKD',str(value or '').casefold())
 return ' '.join(''.join(ch for ch in value if not unicodedata.combining(ch)).split())

NORMALIZED_ALIASES={normalized_name(name):code for name,code in TEAM_ALIASES.items()}

def team_code(info):
 t=(info or {}).get('team',info or {})
 if not isinstance(t,dict):return None
 abbr=str(t.get('abbreviation','')).strip().upper()
 if abbr in ALL_TEAMS:return abbr
 for field in ('name','teamName','clubName','shortName','locationName'):
  name=normalized_name(t.get(field,''))
  if name in NORMALIZED_ALIASES:return NORMALIZED_ALIASES[name]
  # Prefer whole-word phrases, never arbitrary substring collisions.
  for match,code in NORMALIZED_ALIASES.items():
   if match and (' '+match+' ') in (' '+name+' '):return code
 return None

def games_back(leader,other):
 # Diferencia matemática contra el equipo que encabeza la tabla completa.
 value=((leader['wins']-other['wins'])+(other['losses']-leader['losses']))/2
 return '—' if other is leader else f'{max(0,value):.1f}'

def get(path,params):
 r=requests.get(BASE+path,params=params,headers={'User-Agent':'SurWakeUpAnalytics/3.180 (LIDOM scores)'},timeout=20)
 r.raise_for_status()
 data=r.json()
 if not isinstance(data,dict):raise ValueError('Respuesta de proveedor no es objeto JSON')
 return data

def normalized_state(g):
 status=g.get('status') or {}
 abstract=str(status.get('abstractGameState','')).lower()
 detailed=str(status.get('detailedState','')).lower()
 coded=str(status.get('codedGameState','')).upper()
 if abstract=='final' or detailed.startswith('final') or coded=='F':return 'final'
 if abstract=='live' or detailed in ('in progress','manager challenge','delayed','suspended') or coded in ('I','M'):return 'live'
 return 'scheduled'

def game_detail(g,state):
 lines=g.get('linescore') or {}
 inning=lines.get('currentInningOrdinal') or lines.get('currentInning')
 half=lines.get('inningHalf')
 if state=='live':return (' '.join(str(x) for x in (half,inning) if x) or 'En desarrollo')[:65]
 if state=='final':return str(g.get('status',{}).get('detailedState','Final'))[:65]
 return 'Hora RD: '+datetime.fromisoformat(g['gameDate'].replace('Z','+00:00')).astimezone(ZONE).strftime('%I:%M %p')

def transform(schedule,standings,now):
 today=now.date().isoformat()
 games=[]
 seen=set()
 for date in schedule.get('dates',[]):
  for g in date.get('games',[]):
   pk=g.get('gamePk')
   if pk in seen:continue
   seen.add(pk)
   tm=g.get('teams') or {};a=team_code(tm.get('away'));h=team_code(tm.get('home'))
   if not a or not h or a==h:continue
   date_utc=g.get('gameDate')
   if not date_utc:continue
   local=datetime.fromisoformat(date_utc.replace('Z','+00:00')).astimezone(ZONE)
   if not (now.date()-timedelta(days=1) <= local.date() <= now.date()+timedelta(days=1)):continue
   state=normalized_state(g)
   away=tm['away'].get('score');home=tm['home'].get('score')
   scores_valid=isinstance(away,int) and isinstance(home,int)
   games.append({'date':local.strftime('%d/%m/%Y %I:%M %p'),'local_game_date':local.date().isoformat(), 'away':a,'home':h,'state':state,
    'away_score':away if state!='scheduled' and scores_valid else None,
    'home_score':home if state!='scheduled' and scores_valid else None,
    'detail':game_detail(g,state),'game_pk':pk})
 games.sort(key=lambda x:(x['local_game_date'],x['date'],x['game_pk']))
 by_team={}
 unknown=[]
 for record in standings.get('records',[]):
  for t in record.get('teamRecords',[]):
   code=team_code(t)
   if not code:
    unknown.append(str((t.get('team') or {}).get('name','Desconocido')))
    continue
   try:w=int(t.get('wins'));l=int(t.get('losses'))
   except (ValueError,TypeError):continue
   if w<0 or l<0:continue
   row={'team':code,'wins':w,'losses':l,'pct':round(w/(w+l),3) if w+l else 0,
    'streak':str((t.get('streak') or {}).get('streakCode','—'))}
   # No duplicar entradas del mismo equipo si la API devuelve varios grupos.
   if code not in by_team or (w+l)>(by_team[code]['wins']+by_team[code]['losses']):
    by_team[code]=row
 rows=sorted(by_team.values(),key=lambda x:(-x['pct'],-x['wins'],x['losses'],x['team']))
 if rows:
  leader=rows[0]
  for row in rows:row['games_back']=games_back(leader,row)
 missing=[code for code in ALL_TEAMS if code not in by_team]

 return {'schema_version':1,'fetched_at_utc':now.astimezone(timezone.utc).isoformat(),
  'source_name':'MLB Stats API · LIDOM (liga 131)', 'source_url':'https://statsapi.mlb.com/api/v1/schedule',
  'local_date':today,'games':games,'standings':rows,
  'standings_complete':not missing,'standings_missing':missing,
  'standings_unrecognized':unknown[:12],
  'note':'Datos sujetos a disponibilidad/correcciones del proveedor; actualizados al ejecutar este programa.'}

def main():
 now=datetime.now(ZONE)
 date=now.date()
 schedule=get('/schedule',{'sportId':17,'leagueId':131,'startDate':(date-timedelta(days=1)).isoformat(),
  'endDate':(date+timedelta(days=1)).isoformat(),'hydrate':'linescore'})
 if 'dates' not in schedule:raise ValueError('La API no incluyó dates')
 standings=get('/standings',{'leagueId':131,'season':now.year,'standingsTypes':'regularSeason'})
 if 'records' not in standings:raise ValueError('La API no incluyó records')
 data=transform(schedule,standings,now)
 DST.parent.mkdir(parents=True,exist_ok=True)
 fd,tmp=tempfile.mkstemp(prefix='.lidom-',suffix='.json',dir=DST.parent)
 try:
  with os.fdopen(fd,'w',encoding='utf-8') as f:json.dump(data,f,ensure_ascii=False,indent=2)
  os.replace(tmp,DST)
  # Copia JS para consultar el último corte también desde index.html abierto con file://.
  # Nunca se declara en vivo un dato histórico: la web muestra su hora real.
  js_dst=DST.with_name('lidom_live_data.js')
  js_dst.write_text('window.SWU_LIDOM_LOCAL_DATA = '+json.dumps(data,ensure_ascii=False).replace('<','\\u003c')+';\n',encoding='utf-8')
 finally:
  if os.path.exists(tmp):os.unlink(tmp)
 print('OK',data['local_date'],len(data['games']),'partidos',len(data['standings']),'equipos')
 if data['standings_missing']:
  print('AVISO: tabla incompleta; faltan:', ', '.join(data['standings_missing']))
 if data['standings_unrecognized']:
  print('AVISO: nombres no reconocidos:', ', '.join(data['standings_unrecognized']))

if __name__=='__main__':
 try:main()
 except Exception as e:
  print('ERROR: se conserva el último JSON sin modificar:',str(e),file=sys.stderr)
  sys.exit(1)
