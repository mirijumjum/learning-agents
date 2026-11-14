extends SceneTree
## Simulación de balance sin interfaz:
##   godot --headless --path godot -s res://sim/simular.gd -- --partidas=500 --salida=<ruta.json>
## Juega N partidas con la lógica del juego (clases puras, sin escenas) y escribe una lista
## de métricas por partida. Lo usa `python tools/estudio.py sim`, que resume los números
## para el economista.


func _init() -> void:
	var args := {}
	for arg in OS.get_cmdline_user_args():
		if arg.begins_with("--") and arg.contains("="):
			var partes := arg.substr(2).split("=", true, 1)
			args[partes[0]] = partes[1]
	var partidas := int(args.get("partidas", "200"))
	var salida: String = args.get("salida", "user://simulacion.json")

	var resultados: Array = []
	for semilla in partidas:
		resultados.append(jugar_partida(semilla))

	var archivo := FileAccess.open(salida, FileAccess.WRITE)
	if archivo == null:
		push_error("No se puede escribir %s" % salida)
		quit(1)
		return
	archivo.store_string(JSON.stringify({"partidas": resultados}, "  "))
	archivo.close()
	print("Simuladas %d partidas → %s" % [partidas, salida])
	quit(0)


## SUSTITUIR por la lógica real del juego: crea el estado con las clases puras, toma
## decisiones con un bot sencillo (aleatorio, codicioso…) hasta que la partida acabe y
## devuelve sus métricas. Deben ser números o booleanos: las del GDD, «Métricas de balance».
func jugar_partida(semilla: int) -> Dictionary:
	var rng := RandomNumberGenerator.new()
	rng.seed = semilla
	var recursos := 10
	var turnos := 0
	while recursos > 0 and turnos < 100:
		recursos += rng.randi_range(-3, 3)
		turnos += 1
	return {
		"semilla": semilla,
		"turnos": turnos,
		"recursos_finales": recursos,
		"victoria": turnos >= 100
	}
