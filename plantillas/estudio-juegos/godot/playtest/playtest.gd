extends Node
## Autoload de playtest. No hace nada salvo que el juego se lance con:
##   godot --path godot --fixed-fps 60 -- --playtest-guion=<ruta.json> --playtest-salida=<carpeta>
## Entonces pulsa y suelta las acciones del guion en los fotogramas indicados, guarda capturas
## y cierra el juego al terminar. Lo usa `python tools/estudio.py playtest`.

var _guion: Dictionary = {}
var _eventos: Array = []
var _salida := ""
var _frame := 0
var _activo := false


func _ready() -> void:
	var args := _leer_argumentos()
	if not args.has("playtest-guion"):
		return
	var datos: Variant = JSON.parse_string(FileAccess.get_file_as_string(args["playtest-guion"]))
	if typeof(datos) != TYPE_DICTIONARY:
		push_error("Playtest: el guion no es un JSON válido")
		get_tree().quit(2)
		return
	_guion = datos
	_eventos = _guion.get("eventos", [])
	_salida = args.get("playtest-salida", OS.get_user_data_dir())
	DirAccess.make_dir_recursive_absolute(_salida)
	process_mode = Node.PROCESS_MODE_ALWAYS
	_activo = true
	print(
		"Playtest: %d eventos, %d fotogramas" % [_eventos.size(), int(_guion.get("duracion", 600))]
	)


func _leer_argumentos() -> Dictionary:
	var resultado := {}
	for arg in OS.get_cmdline_user_args():
		if arg.begins_with("--") and arg.contains("="):
			var partes := arg.substr(2).split("=", true, 1)
			resultado[partes[0]] = partes[1]
	return resultado


func _process(_delta: float) -> void:
	if not _activo:
		return
	_frame += 1
	for evento in _eventos:
		if int(evento.get("frame", -1)) == _frame:
			var accion := InputEventAction.new()
			accion.action = StringName(evento.get("accion", ""))
			accion.pressed = bool(evento.get("pulsado", true))
			Input.parse_input_event(accion)
	var cada := int(_guion.get("capturas_cada", 120))
	if cada > 0 and _frame % cada == 0:
		_capturar()
	if _frame >= int(_guion.get("duracion", 600)):
		_capturar()
		_activo = false
		get_tree().quit(0)


func _capturar() -> void:
	var imagen := get_viewport().get_texture().get_image()
	imagen.save_png(_salida.path_join("captura_%05d.png" % _frame))
